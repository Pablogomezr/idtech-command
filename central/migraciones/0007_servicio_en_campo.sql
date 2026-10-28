-- 0007 — Servicio en campo (app del técnico): llegada con ubicación, lista de pasos por
-- falla, fotos, firma de quien recibe y acta.
--
-- Ubicación: SOLO en el momento de marcar la llegada, nunca rastreo continuo (ADR 0003). Se
-- guarda la distancia al sitio; las coordenadas del técnico, redondeadas a ~11 m.
-- Archivos: el contenido vive fuera de la base (disco del servidor; Object Storage Bogotá en
-- producción). Aquí solo su huella SHA-256, tamaño y tipo.

ALTER TABLE idcmd.tickets
    ADD COLUMN llegada_lat          numeric(8, 4),
    ADD COLUMN llegada_lon          numeric(8, 4),
    ADD COLUMN llegada_precision_m  integer,
    ADD COLUMN llegada_distancia_m  integer,
    ADD COLUMN recibido_por         text,
    ADD COLUMN firmado_at           timestamptz;

-- Pasos sugeridos de la base de conocimiento, copiados al ticket al abrirlo: el técnico marca
-- cada uno como hecho o "no aplica". Sin pasos pendientes no se puede marcar "Resuelto".
CREATE TABLE idcmd.pasos_ticket (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id   uuid NOT NULL REFERENCES idcmd.tickets,
    cliente_id  uuid NOT NULL,
    sitio_id    uuid NOT NULL,
    codigo      text NOT NULL,          -- falla que lo originó
    orden       integer NOT NULL,
    texto       text NOT NULL,
    estado      text NOT NULL DEFAULT 'pendiente'
                CHECK (estado IN ('pendiente', 'hecho', 'no_aplica')),
    marcado_at  timestamptz,
    usuario_id  uuid REFERENCES idcmd.usuarios,
    UNIQUE (ticket_id, codigo, orden)
);
CREATE INDEX ON idcmd.pasos_ticket (ticket_id);

CREATE TABLE idcmd.archivos_ticket (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id   uuid NOT NULL REFERENCES idcmd.tickets,
    cliente_id  uuid NOT NULL,
    sitio_id    uuid NOT NULL,
    tipo        text NOT NULL CHECK (tipo IN ('foto', 'firma')),
    mime        text NOT NULL CHECK (mime IN ('image/jpeg', 'image/png')),
    bytes       integer NOT NULL CHECK (bytes > 0 AND bytes <= 8388608),
    sha256      bytea NOT NULL CHECK (octet_length(sha256) = 32),
    nota        text,
    creado_at   timestamptz NOT NULL DEFAULT now(),
    usuario_id  uuid NOT NULL REFERENCES idcmd.usuarios
);
CREATE INDEX ON idcmd.archivos_ticket (ticket_id);

ALTER TABLE idcmd.pasos_ticket ENABLE ROW LEVEL SECURITY;
ALTER TABLE idcmd.archivos_ticket ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON idcmd.pasos_ticket, idcmd.archivos_ticket TO idcmd_app;
GRANT SELECT, INSERT ON idcmd.pasos_ticket TO idcmd_worker;
GRANT SELECT ON idcmd.archivos_ticket TO idcmd_worker;
GRANT SELECT ON idcmd.metricas_actuales TO idcmd_worker;
CREATE POLICY worker ON idcmd.pasos_ticket FOR ALL TO idcmd_worker USING (true) WITH CHECK (true);
CREATE POLICY worker ON idcmd.archivos_ticket FOR SELECT TO idcmd_worker USING (true);
CREATE POLICY worker ON idcmd.metricas_actuales FOR SELECT TO idcmd_worker USING (true);
CREATE POLICY lectura ON idcmd.pasos_ticket FOR SELECT TO idcmd_app
    USING (sitio_id IN (SELECT idcmd.sitios_visibles()));
CREATE POLICY lectura ON idcmd.archivos_ticket FOR SELECT TO idcmd_app
    USING (sitio_id IN (SELECT idcmd.sitios_visibles()));

-- Quién puede trabajar un ticket desde el campo: el técnico asignado o un administrador.
CREATE FUNCTION idcmd._puede_trabajar(t idcmd.tickets) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, idcmd AS $$
    SELECT t.sitio_id IN (SELECT idcmd.sitios_visibles())
       AND (idcmd.rol_actual() = 'admin'
            OR (idcmd.rol_actual() = 'tecnico' AND t.tecnico_id = idcmd.usuario_actual()))
       AND t.estado IN ('aceptado', 'en_camino', 'en_sitio')
$$;

-- Llegada al sitio con ubicación (opcional: el técnico puede negarla; queda constancia).
CREATE FUNCTION idcmd.llegada_ticket(
    p_ticket uuid, p_lat double precision, p_lon double precision, p_precision_m integer
) RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, idcmd AS $$
DECLARE
    t idcmd.tickets%ROWTYPE;
    s idcmd.sitios%ROWTYPE;
    distancia integer;
    texto text;
BEGIN
    SELECT * INTO t FROM idcmd.tickets WHERE id = p_ticket FOR UPDATE;
    IF NOT FOUND OR NOT idcmd._puede_trabajar(t) THEN
        RAISE EXCEPTION 'no puede registrar la llegada en este ticket'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT * INTO s FROM idcmd.sitios WHERE id = t.sitio_id;
    IF p_lat IS NOT NULL AND p_lon IS NOT NULL AND s.lat IS NOT NULL AND s.lon IS NOT NULL THEN
        -- Haversine en metros (radio medio de la Tierra).
        distancia := round(2 * 6371000 * asin(sqrt(
            power(sin(radians(p_lat - s.lat::float8) / 2), 2)
            + cos(radians(s.lat::float8)) * cos(radians(p_lat))
              * power(sin(radians(p_lon - s.lon::float8) / 2), 2))));
    END IF;
    UPDATE idcmd.tickets
       SET estado = 'en_sitio', en_sitio_at = now(),
           llegada_lat = round(p_lat::numeric, 4), llegada_lon = round(p_lon::numeric, 4),
           llegada_precision_m = p_precision_m, llegada_distancia_m = distancia
     WHERE id = t.id;
    texto := CASE
        WHEN p_lat IS NULL THEN 'El técnico llegó al sitio (no compartió su ubicación)'
        WHEN distancia IS NULL THEN 'El técnico llegó al sitio (ubicación registrada)'
        WHEN distancia <= s.radio_checkin_m THEN
            format('El técnico llegó al sitio (ubicación confirmada a %s m)', distancia)
        ELSE format('El técnico marcó la llegada a %s m del sitio (fuera del radio de %s m)',
                    distancia, s.radio_checkin_m)
    END;
    INSERT INTO idcmd.eventos_ticket (ticket_id, cliente_id, sitio_id, at, tipo, texto, usuario_id)
    VALUES (t.id, t.cliente_id, t.sitio_id, now(), 'en_sitio', texto, idcmd.usuario_actual());
    RETURN distancia;
END $$;

CREATE FUNCTION idcmd.marcar_paso(p_paso uuid, p_estado text) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, idcmd AS $$
DECLARE
    t idcmd.tickets%ROWTYPE;
BEGIN
    IF p_estado NOT IN ('pendiente', 'hecho', 'no_aplica') THEN
        RAISE EXCEPTION 'estado de paso inválido' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT tk.* INTO t FROM idcmd.tickets tk JOIN idcmd.pasos_ticket p ON p.ticket_id = tk.id
     WHERE p.id = p_paso;
    IF NOT FOUND OR NOT idcmd._puede_trabajar(t) THEN
        RAISE EXCEPTION 'no puede modificar este ticket' USING ERRCODE = 'insufficient_privilege';
    END IF;
    UPDATE idcmd.pasos_ticket
       SET estado = p_estado,
           marcado_at = CASE WHEN p_estado = 'pendiente' THEN NULL ELSE now() END,
           usuario_id = CASE WHEN p_estado = 'pendiente' THEN NULL ELSE idcmd.usuario_actual() END
     WHERE id = p_paso;
END $$;

-- Registra una foto o la firma (el archivo ya quedó guardado por la aplicación con su hash).
CREATE FUNCTION idcmd.registrar_archivo(
    p_ticket uuid, p_tipo text, p_mime text, p_bytes integer, p_sha256 bytea,
    p_nota text DEFAULT NULL, p_recibido_por text DEFAULT NULL
) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, idcmd AS $$
DECLARE
    t idcmd.tickets%ROWTYPE;
    nuevo uuid;
BEGIN
    SELECT * INTO t FROM idcmd.tickets WHERE id = p_ticket FOR UPDATE;
    IF NOT FOUND OR NOT idcmd._puede_trabajar(t) THEN
        RAISE EXCEPTION 'no puede agregar archivos a este ticket'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF p_tipo = 'firma' AND coalesce(btrim(p_recibido_por), '') = '' THEN
        RAISE EXCEPTION 'escriba el nombre de quien recibe' USING ERRCODE = 'check_violation';
    END IF;
    INSERT INTO idcmd.archivos_ticket (ticket_id, cliente_id, sitio_id, tipo, mime, bytes,
                                       sha256, nota, usuario_id)
    VALUES (t.id, t.cliente_id, t.sitio_id, p_tipo, p_mime, p_bytes, p_sha256,
            left(btrim(p_nota), 300), idcmd.usuario_actual())
    RETURNING id INTO nuevo;
    IF p_tipo = 'firma' THEN
        UPDATE idcmd.tickets SET recibido_por = left(btrim(p_recibido_por), 120),
               firmado_at = now() WHERE id = t.id;
        INSERT INTO idcmd.eventos_ticket (ticket_id, cliente_id, sitio_id, at, tipo, texto,
                                          usuario_id)
        VALUES (t.id, t.cliente_id, t.sitio_id, now(), 'firma',
                'Firmó el recibido: ' || left(btrim(p_recibido_por), 120), idcmd.usuario_actual());
    ELSE
        INSERT INTO idcmd.eventos_ticket (ticket_id, cliente_id, sitio_id, at, tipo, texto,
                                          usuario_id)
        VALUES (t.id, t.cliente_id, t.sitio_id, now(), 'foto', 'Se agregó una foto del trabajo',
                idcmd.usuario_actual());
    END IF;
    RETURN nuevo;
END $$;

REVOKE EXECUTE ON FUNCTION idcmd._puede_trabajar(idcmd.tickets),
    idcmd.llegada_ticket(uuid, double precision, double precision, integer),
    idcmd.marcar_paso(uuid, text),
    idcmd.registrar_archivo(uuid, text, text, integer, bytea, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION idcmd.llegada_ticket(uuid, double precision, double precision, integer),
    idcmd.marcar_paso(uuid, text),
    idcmd.registrar_archivo(uuid, text, text, integer, bytea, text, text) TO idcmd_app;

-- "Resuelto" exige la lista de pasos completa (hecho o no aplica).
CREATE FUNCTION idcmd.validar_resuelto() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, idcmd AS $$
BEGIN
    IF NEW.estado = 'resuelto' AND OLD.estado IS DISTINCT FROM 'resuelto'
       AND EXISTS (SELECT 1 FROM idcmd.pasos_ticket WHERE ticket_id = NEW.id
                    AND estado = 'pendiente') THEN
        RAISE EXCEPTION 'complete la lista de pasos (hecho o no aplica) antes de marcar resuelto'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER pasos_completos BEFORE UPDATE OF estado ON idcmd.tickets
    FOR EACH ROW EXECUTE FUNCTION idcmd.validar_resuelto();

-- Acta del servicio para el enlace de seguimiento (sin sesión): pasos, fotos y firma.
CREATE FUNCTION idcmd.acta_ticket(p_ticket uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, idcmd AS $$
    SELECT jsonb_build_object(
        'sitio_id', t.sitio_id,
        'recibido_por', t.recibido_por,
        'firmado_at', t.firmado_at,
        'en_sitio_at', t.en_sitio_at,
        'llegada_distancia_m', t.llegada_distancia_m,
        'pasos', coalesce((SELECT jsonb_agg(jsonb_build_object('texto', p.texto, 'estado', p.estado)
                                             ORDER BY p.codigo, p.orden)
                             FROM idcmd.pasos_ticket p WHERE p.ticket_id = t.id), '[]'),
        'archivos', coalesce((SELECT jsonb_agg(jsonb_build_object('id', a.id, 'tipo', a.tipo,
                                               'mime', a.mime, 'sha256', encode(a.sha256, 'hex'),
                                               'nota', a.nota, 'creado_at', a.creado_at)
                                               ORDER BY a.creado_at)
                                FROM idcmd.archivos_ticket a WHERE a.ticket_id = t.id), '[]'))
      FROM idcmd.tickets t
     WHERE t.id = p_ticket
       AND (t.cerrado_at IS NULL OR t.cerrado_at > now() - interval '30 days')
$$;
REVOKE EXECUTE ON FUNCTION idcmd.acta_ticket(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION idcmd.acta_ticket(uuid) TO idcmd_app;
