-- 0006 — Módulo de servicio: tickets automáticos, asignación, escalamiento, SLA, avisos y
-- seguimiento para el cliente.
--
-- Flujo: el agente confirma una falla -> el worker abre el ticket (uno por equipo) -> lo
-- asigna al técnico principal del sitio -> si no acepta a tiempo, al suplente y luego a los
-- administradores -> el técnico avanza los estados desde el portal -> al marcar "resuelto",
-- el worker exige que el equipo lleve 10 min sano para cerrarlo (reverificación, ADR 0004).
--
-- SLA: respuesta = el técnico acepta el ticket; solución = el equipo verificado sano.
-- Calendario 24/7 (decisión de Pablo, 2026-10-28).

CREATE TYPE idcmd.prioridad AS ENUM ('critica', 'alta', 'media', 'baja');
CREATE TYPE idcmd.estado_ticket AS ENUM (
    'abierto', 'asignado', 'aceptado', 'en_camino', 'en_sitio', 'resuelto', 'cerrado', 'cancelado'
);

-- Tiempos comprometidos por prioridad, en minutos. Una fila por cliente las sobrescribe;
-- cliente_id NULL = valores por defecto de IDtech.
CREATE TABLE idcmd.reglas_sla (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cliente_id       uuid REFERENCES idcmd.clientes,
    prioridad        idcmd.prioridad NOT NULL,
    respuesta_min    integer NOT NULL CHECK (respuesta_min > 0),
    solucion_min     integer NOT NULL CHECK (solucion_min >= respuesta_min),
    escalar_min      integer NOT NULL CHECK (escalar_min > 0),  -- sin aceptar -> siguiente
    UNIQUE NULLS NOT DISTINCT (cliente_id, prioridad)
);
INSERT INTO idcmd.reglas_sla (cliente_id, prioridad, respuesta_min, solucion_min, escalar_min)
VALUES (NULL, 'critica', 120, 480, 15),
       (NULL, 'alta', 240, 1440, 30),
       (NULL, 'media', 1440, 4320, 120),
       (NULL, 'baja', 4320, 10080, 480);

-- Personas del cliente que siguen los tickets de un sitio (no son usuarios del portal).
CREATE TABLE idcmd.contactos_sitio (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cliente_id       uuid NOT NULL,
    sitio_id         uuid NOT NULL,
    nombre           text NOT NULL,
    correo           citext,
    telegram_chat_id bigint,
    activo           boolean NOT NULL DEFAULT true,
    CHECK (correo IS NOT NULL OR telegram_chat_id IS NOT NULL),
    FOREIGN KEY (sitio_id, cliente_id) REFERENCES idcmd.sitios (id, cliente_id),
    UNIQUE (sitio_id, correo)
);

ALTER TABLE idcmd.usuarios ADD COLUMN telegram_chat_id bigint;

CREATE TABLE idcmd.tickets (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    numero             bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    cliente_id         uuid NOT NULL,
    sitio_id           uuid NOT NULL,
    dispositivo_id     uuid,           -- NULL: falla del sitio (p. ej. sin conexión)
    origen             text NOT NULL CHECK (origen IN ('automatico', 'manual')),
    tipo               text NOT NULL CHECK (tipo IN ('correctivo', 'preventivo')),
    prioridad          idcmd.prioridad NOT NULL,
    codigo             text NOT NULL CHECK (codigo ~ '^[a-z0-9_]{1,64}$'),
    titulo             text NOT NULL,
    estado             idcmd.estado_ticket NOT NULL DEFAULT 'abierto',
    tecnico_id         uuid REFERENCES idcmd.usuarios,
    nivel_escalamiento smallint NOT NULL DEFAULT 0,   -- 0 principal, 1 suplente, 2 admins
    abierto_at         timestamptz NOT NULL,          -- inicio de la falla (hora del agente)
    asignado_at        timestamptz,
    aceptado_at        timestamptz,
    en_sitio_at        timestamptz,
    resuelto_at        timestamptz,
    cerrado_at         timestamptz,
    vence_respuesta_at timestamptz NOT NULL,
    vence_solucion_at  timestamptz NOT NULL,
    cierre             text CHECK (cierre IN ('verificado', 'recuperado_solo', 'cancelado')),
    FOREIGN KEY (sitio_id, cliente_id) REFERENCES idcmd.sitios (id, cliente_id),
    FOREIGN KEY (dispositivo_id, sitio_id, cliente_id)
        REFERENCES idcmd.dispositivos (id, sitio_id, cliente_id)
);
CREATE INDEX ON idcmd.tickets (sitio_id, estado);
CREATE INDEX ON idcmd.tickets (tecnico_id, estado);
-- Un solo ticket abierto por equipo (y por sitio, para las fallas del sitio): las fallas
-- nuevas del mismo equipo se agregan a su línea de tiempo.
CREATE UNIQUE INDEX un_ticket_abierto_por_equipo ON idcmd.tickets (dispositivo_id)
    WHERE estado NOT IN ('cerrado', 'cancelado') AND dispositivo_id IS NOT NULL;
CREATE UNIQUE INDEX un_ticket_abierto_por_sitio ON idcmd.tickets (sitio_id, codigo)
    WHERE estado NOT IN ('cerrado', 'cancelado') AND dispositivo_id IS NULL;

-- Línea de tiempo: solo se agrega. `visible_cliente` decide qué ve el enlace de seguimiento.
CREATE TABLE idcmd.eventos_ticket (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticket_id       uuid NOT NULL REFERENCES idcmd.tickets,
    cliente_id      uuid NOT NULL,
    sitio_id        uuid NOT NULL,
    at              timestamptz NOT NULL,
    tipo            text NOT NULL,
    texto           text NOT NULL,
    usuario_id      uuid REFERENCES idcmd.usuarios,
    visible_cliente boolean NOT NULL DEFAULT true
);
CREATE INDEX ON idcmd.eventos_ticket (ticket_id, id);

-- Avisos por enviar (bandeja de salida del worker). Solo nombre de equipo, sitio, estado y
-- enlace (principio 5): nunca IPs ni detalles técnicos.
CREATE TABLE idcmd.notificaciones (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticket_id   uuid REFERENCES idcmd.tickets,
    cliente_id  uuid NOT NULL,
    sitio_id    uuid NOT NULL,
    canal       text NOT NULL CHECK (canal IN ('correo', 'telegram')),
    destino     text NOT NULL,
    asunto      text NOT NULL,
    cuerpo      text NOT NULL,
    creada_at   timestamptz NOT NULL DEFAULT now(),
    enviada_at  timestamptz,
    intentos    integer NOT NULL DEFAULT 0,
    resultado   text   -- 'enviada', 'sin_canal' (no configurado), o el error
);
CREATE INDEX ON idcmd.notificaciones (enviada_at) WHERE enviada_at IS NULL;

-- Cursor del worker sobre la línea de tiempo (qué eventos ya generaron avisos).
CREATE TABLE idcmd.cursor_worker (
    nombre text PRIMARY KEY,
    valor bigint NOT NULL
);

-- Rol del worker: ve todos los sitios (abre y asigna tickets de cualquiera) pero solo en
-- estas tablas, y sin BYPASSRLS. -------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'idcmd_worker') THEN
        CREATE ROLE idcmd_worker NOLOGIN NOBYPASSRLS;
    END IF;
END $$;
GRANT USAGE ON SCHEMA idcmd TO idcmd_worker;
GRANT SELECT ON idcmd.sitios, idcmd.clientes, idcmd.dispositivos, idcmd.estado_actual,
    idcmd.asignaciones_tecnico, idcmd.reglas_sla, idcmd.contactos_sitio TO idcmd_worker;
GRANT SELECT (id, sitio_id, revocado_at, ultimo_latido_at) ON idcmd.agentes TO idcmd_worker;
GRANT SELECT (id, correo, nombre, rol, activo, telegram_chat_id) ON idcmd.usuarios
    TO idcmd_worker;
GRANT SELECT, INSERT, UPDATE ON idcmd.tickets, idcmd.notificaciones, idcmd.cursor_worker
    TO idcmd_worker;
GRANT SELECT, INSERT ON idcmd.eventos_ticket TO idcmd_worker;

ALTER TABLE idcmd.reglas_sla ENABLE ROW LEVEL SECURITY;
ALTER TABLE idcmd.contactos_sitio ENABLE ROW LEVEL SECURITY;
ALTER TABLE idcmd.tickets ENABLE ROW LEVEL SECURITY;
ALTER TABLE idcmd.eventos_ticket ENABLE ROW LEVEL SECURITY;
ALTER TABLE idcmd.notificaciones ENABLE ROW LEVEL SECURITY;
ALTER TABLE idcmd.cursor_worker ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['sitios', 'clientes', 'dispositivos', 'estado_actual', 'agentes',
        'usuarios', 'asignaciones_tecnico', 'reglas_sla', 'contactos_sitio', 'tickets',
        'eventos_ticket', 'notificaciones', 'cursor_worker']
    LOOP
        EXECUTE format('CREATE POLICY worker ON idcmd.%I FOR ALL TO idcmd_worker'
                       ' USING (true) WITH CHECK (true)', t);
    END LOOP;
END $$;

-- Portal: lectura según alcance; las acciones sobre tickets van SOLO por función. ----------
GRANT SELECT ON idcmd.tickets, idcmd.eventos_ticket, idcmd.reglas_sla, idcmd.contactos_sitio
    TO idcmd_app;
GRANT SELECT ON idcmd.notificaciones TO idcmd_app;
GRANT INSERT, UPDATE, DELETE ON idcmd.contactos_sitio, idcmd.reglas_sla TO idcmd_app;
GRANT UPDATE (telegram_chat_id) ON idcmd.usuarios TO idcmd_app;

CREATE POLICY lectura ON idcmd.tickets FOR SELECT TO idcmd_app
    USING (sitio_id IN (SELECT idcmd.sitios_visibles()));
CREATE POLICY lectura ON idcmd.eventos_ticket FOR SELECT TO idcmd_app
    USING (sitio_id IN (SELECT idcmd.sitios_visibles())
           AND (visible_cliente OR idcmd.rol_actual() IN ('admin', 'tecnico')));
CREATE POLICY lectura ON idcmd.contactos_sitio FOR SELECT TO idcmd_app
    USING (sitio_id IN (SELECT idcmd.sitios_visibles()));
CREATE POLICY admin_escribe ON idcmd.contactos_sitio FOR ALL TO idcmd_app
    USING (idcmd.es_admin()) WITH CHECK (idcmd.es_admin());
CREATE POLICY lectura ON idcmd.reglas_sla FOR SELECT TO idcmd_app USING (true);
CREATE POLICY admin_escribe ON idcmd.reglas_sla FOR ALL TO idcmd_app
    USING (idcmd.es_admin()) WITH CHECK (idcmd.es_admin());
CREATE POLICY lectura ON idcmd.notificaciones FOR SELECT TO idcmd_app USING (idcmd.es_admin());

-- Acciones del portal sobre un ticket. Valida quién y qué transición; deja el evento en la
-- línea de tiempo. Admin: cualquier ticket. Técnico: solo los asignados a él, o "tomar" uno
-- sin aceptar de un sitio que tiene asignado. Cliente: nada (solo mira).
CREATE FUNCTION idcmd.accion_ticket(p_ticket uuid, p_accion text, p_nota text DEFAULT NULL)
RETURNS idcmd.estado_ticket
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, idcmd AS $$
DECLARE
    t idcmd.tickets%ROWTYPE;
    yo uuid := idcmd.usuario_actual();
    rol idcmd.rol := idcmd.rol_actual();
    nuevo idcmd.estado_ticket;
    texto text;
    ahora timestamptz := now();
BEGIN
    SELECT * INTO t FROM idcmd.tickets WHERE id = p_ticket FOR UPDATE;
    IF NOT FOUND OR t.sitio_id NOT IN (SELECT idcmd.sitios_visibles()) THEN
        RAISE EXCEPTION 'ticket no encontrado' USING ERRCODE = 'no_data_found';
    END IF;
    IF rol IS NULL OR rol = 'cliente' THEN
        RAISE EXCEPTION 'sin permiso' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF rol = 'tecnico' AND p_accion NOT IN ('tomar', 'nota')
       AND t.tecnico_id IS DISTINCT FROM yo THEN
        RAISE EXCEPTION 'el ticket está asignado a otra persona' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF p_accion = 'cancelar' AND rol <> 'admin' THEN
        RAISE EXCEPTION 'solo un administrador cancela tickets' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF t.estado IN ('cerrado', 'cancelado') AND p_accion <> 'nota' THEN
        RAISE EXCEPTION 'el ticket ya está cerrado' USING ERRCODE = 'check_violation';
    END IF;

    CASE p_accion
        WHEN 'tomar' THEN
            IF t.estado NOT IN ('abierto', 'asignado') THEN
                RAISE EXCEPTION 'el ticket ya fue aceptado' USING ERRCODE = 'check_violation';
            END IF;
            nuevo := 'aceptado';
            UPDATE idcmd.tickets SET tecnico_id = yo, estado = nuevo,
                   asignado_at = coalesce(asignado_at, ahora), aceptado_at = ahora
             WHERE id = t.id;
            texto := 'Un técnico tomó el servicio';
        WHEN 'aceptar' THEN
            IF t.estado <> 'asignado' THEN
                RAISE EXCEPTION 'solo se acepta un ticket asignado' USING ERRCODE = 'check_violation';
            END IF;
            nuevo := 'aceptado';
            UPDATE idcmd.tickets SET estado = nuevo, aceptado_at = ahora WHERE id = t.id;
            texto := 'El técnico aceptó el servicio';
        WHEN 'en_camino' THEN
            IF t.estado <> 'aceptado' THEN
                RAISE EXCEPTION 'primero acepte el ticket' USING ERRCODE = 'check_violation';
            END IF;
            nuevo := 'en_camino';
            UPDATE idcmd.tickets SET estado = nuevo WHERE id = t.id;
            texto := 'El técnico va en camino';
        WHEN 'en_sitio' THEN
            IF t.estado NOT IN ('aceptado', 'en_camino') THEN
                RAISE EXCEPTION 'primero acepte el ticket' USING ERRCODE = 'check_violation';
            END IF;
            nuevo := 'en_sitio';
            UPDATE idcmd.tickets SET estado = nuevo, en_sitio_at = ahora WHERE id = t.id;
            texto := 'El técnico llegó al sitio';
        WHEN 'resuelto' THEN
            IF t.estado NOT IN ('aceptado', 'en_camino', 'en_sitio') THEN
                RAISE EXCEPTION 'primero acepte el ticket' USING ERRCODE = 'check_violation';
            END IF;
            nuevo := 'resuelto';
            UPDATE idcmd.tickets SET estado = nuevo, resuelto_at = ahora WHERE id = t.id;
            texto := 'El técnico reportó la falla resuelta; el sistema verificará el equipo';
        WHEN 'cancelar' THEN
            nuevo := 'cancelado';
            UPDATE idcmd.tickets SET estado = nuevo, cerrado_at = ahora, cierre = 'cancelado'
             WHERE id = t.id;
            texto := 'Servicio cancelado';
        WHEN 'nota' THEN
            IF coalesce(btrim(p_nota), '') = '' THEN
                RAISE EXCEPTION 'la nota está vacía' USING ERRCODE = 'check_violation';
            END IF;
            nuevo := t.estado;
            texto := 'Nota';
        ELSE
            RAISE EXCEPTION 'acción desconocida: %', p_accion USING ERRCODE = 'invalid_parameter_value';
    END CASE;

    INSERT INTO idcmd.eventos_ticket (ticket_id, cliente_id, sitio_id, at, tipo, texto,
                                      usuario_id, visible_cliente)
    VALUES (t.id, t.cliente_id, t.sitio_id, ahora, p_accion,
            CASE WHEN p_nota IS NULL OR btrim(p_nota) = '' THEN texto
                 ELSE texto || ': ' || left(btrim(p_nota), 1000) END,
            yo, p_accion <> 'nota');  -- las notas son internas
    RETURN nuevo;
END $$;
REVOKE EXECUTE ON FUNCTION idcmd.accion_ticket(uuid, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION idcmd.accion_ticket(uuid, text, text) TO idcmd_app;

-- Enlace de seguimiento del cliente (sin sesión): resumen y línea de tiempo visible. El
-- enlace lleva un HMAC del id que valida la aplicación; esta función solo entrega datos
-- sin IPs ni detalles técnicos, y nada de tickets cerrados hace más de 30 días.
CREATE FUNCTION idcmd.seguimiento_ticket(p_ticket uuid)
RETURNS TABLE (numero bigint, sitio text, equipo text, titulo text, estado idcmd.estado_ticket,
               prioridad idcmd.prioridad, abierto_at timestamptz, aceptado_at timestamptz,
               cerrado_at timestamptz, vence_respuesta_at timestamptz,
               vence_solucion_at timestamptz, tecnico text, eventos jsonb)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, idcmd AS $$
    SELECT t.numero, s.nombre, d.nombre, t.titulo, t.estado, t.prioridad, t.abierto_at,
           t.aceptado_at, t.cerrado_at, t.vence_respuesta_at, t.vence_solucion_at,
           split_part(u.nombre, ' ', 1),
           coalesce((SELECT jsonb_agg(jsonb_build_object('at', e.at, 'texto', e.texto)
                                      ORDER BY e.id)
                       FROM idcmd.eventos_ticket e
                      WHERE e.ticket_id = t.id AND e.visible_cliente), '[]')
      FROM idcmd.tickets t
      JOIN idcmd.sitios s ON s.id = t.sitio_id
      LEFT JOIN idcmd.dispositivos d ON d.id = t.dispositivo_id
      LEFT JOIN idcmd.usuarios u ON u.id = t.tecnico_id
     WHERE t.id = p_ticket
       AND (t.cerrado_at IS NULL OR t.cerrado_at > now() - interval '30 days')
$$;
REVOKE EXECUTE ON FUNCTION idcmd.seguimiento_ticket(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION idcmd.seguimiento_ticket(uuid) TO idcmd_app;
