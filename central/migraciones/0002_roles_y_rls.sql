-- 0002 — Roles de servicio y seguridad a nivel de fila (RLS).
--
-- Roles (de grupo, sin login; el usuario de login de la aplicación es miembro con NOINHERIT
-- y en cada transacción hace SET LOCAL ROLE al que corresponda):
--   idcmd_app      portal: ve solo lo que permite el usuario en `app.usuario_id`.
--   idcmd_ingesta  API de ingesta: escribe solo para el sitio del agente en `app.agente_id`.
-- Ninguno es dueño de las tablas ni tiene BYPASSRLS: la RLS siempre aplica.
--
-- La RLS protege contra errores de la aplicación (un filtro olvidado), no contra el robo
-- de la credencial de la base: quien la tenga puede fijar `app.usuario_id`.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'idcmd_app') THEN
        CREATE ROLE idcmd_app NOLOGIN NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'idcmd_ingesta') THEN
        CREATE ROLE idcmd_ingesta NOLOGIN NOBYPASSRLS;
    END IF;
END $$;

-- Funciones de contexto -------------------------------------------------------------

CREATE FUNCTION idcmd.usuario_actual() RETURNS uuid
LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('app.usuario_id', true), '')::uuid
$$;

CREATE FUNCTION idcmd.agente_actual() RETURNS uuid
LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('app.agente_id', true), '')::uuid
$$;

-- SECURITY DEFINER: leen usuarios y asignaciones sin quedar atrapadas en su propia RLS.
CREATE FUNCTION idcmd.rol_actual() RETURNS idcmd.rol
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, idcmd AS $$
    SELECT rol FROM idcmd.usuarios WHERE id = idcmd.usuario_actual() AND activo
$$;

CREATE FUNCTION idcmd.es_admin() RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT coalesce(idcmd.rol_actual() = 'admin', false)
$$;

CREATE FUNCTION idcmd.sitios_visibles() RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, idcmd AS $$
    -- Admin IDtech: todos los sitios.
    SELECT s.id FROM idcmd.sitios s WHERE idcmd.rol_actual() = 'admin'
    UNION
    -- Técnico: solo los sitios con asignación vigente.
    SELECT a.sitio_id
      FROM idcmd.asignaciones_tecnico a
     WHERE a.tecnico_id = idcmd.usuario_actual()
       AND idcmd.rol_actual() = 'tecnico'
       AND a.vigente_desde <= now()
       AND (a.vigente_hasta IS NULL OR a.vigente_hasta > now())
    UNION
    -- Cliente: su alcance (todo el cliente, una regional o un sitio).
    SELECT s.id
      FROM idcmd.alcances_usuario al
      JOIN idcmd.sitios s ON s.cliente_id = al.cliente_id
                         AND (al.regional_id IS NULL OR s.regional_id = al.regional_id)
                         AND (al.sitio_id IS NULL OR s.id = al.sitio_id)
     WHERE al.usuario_id = idcmd.usuario_actual()
       AND idcmd.rol_actual() = 'cliente'
$$;

CREATE FUNCTION idcmd.clientes_visibles() RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, idcmd AS $$
    SELECT c.id FROM idcmd.clientes c WHERE idcmd.rol_actual() = 'admin'
    UNION
    SELECT s.cliente_id FROM idcmd.sitios s WHERE s.id IN (SELECT idcmd.sitios_visibles())
    UNION
    SELECT al.cliente_id FROM idcmd.alcances_usuario al
     WHERE al.usuario_id = idcmd.usuario_actual() AND idcmd.rol_actual() = 'cliente'
$$;

CREATE FUNCTION idcmd.regionales_visibles() RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, idcmd AS $$
    SELECT r.id FROM idcmd.regionales r WHERE idcmd.rol_actual() = 'admin'
    UNION
    SELECT s.regional_id FROM idcmd.sitios s WHERE s.id IN (SELECT idcmd.sitios_visibles())
$$;

CREATE FUNCTION idcmd.sitio_del_agente() RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, idcmd AS $$
    SELECT sitio_id FROM idcmd.agentes WHERE id = idcmd.agente_actual() AND revocado_at IS NULL
$$;

-- Autenticación del agente por el hash de su token. Única puerta de entrada de la ingesta.
CREATE FUNCTION idcmd.autenticar_agente(p_token_hash bytea)
RETURNS TABLE (agente_id uuid, cliente_id uuid, sitio_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, idcmd AS $$
    SELECT id, cliente_id, sitio_id FROM idcmd.agentes
     WHERE token_hash = p_token_hash AND revocado_at IS NULL
$$;
REVOKE EXECUTE ON FUNCTION idcmd.autenticar_agente(bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION idcmd.autenticar_agente(bytea) TO idcmd_ingesta;

-- Permisos -----------------------------------------------------------------------------

GRANT USAGE ON SCHEMA idcmd TO idcmd_app, idcmd_ingesta;

-- Portal: lectura general, escritura de inventario (la RLS la limita al Admin).
GRANT SELECT ON ALL TABLES IN SCHEMA idcmd TO idcmd_app;
GRANT INSERT, UPDATE, DELETE ON
    idcmd.clientes, idcmd.regionales, idcmd.sitios, idcmd.dispositivos,
    idcmd.alcances_usuario, idcmd.asignaciones_tecnico
    TO idcmd_app;
GRANT INSERT ON idcmd.auditoria_accesos TO idcmd_app;

-- Secretos del portal y de los agentes: el portal puede escribirlos, nunca leerlos.
REVOKE SELECT ON idcmd.agentes, idcmd.usuarios FROM idcmd_app;
GRANT SELECT (id, cliente_id, sitio_id, nombre, creado_at, revocado_at, ultimo_latido_at,
              inicio_sesion, version, pendientes, desfase_reloj_s)
    ON idcmd.agentes TO idcmd_app;
GRANT INSERT (id, cliente_id, sitio_id, nombre, token_hash) ON idcmd.agentes TO idcmd_app;
GRANT UPDATE (nombre, revocado_at) ON idcmd.agentes TO idcmd_app;
GRANT SELECT (id, correo, nombre, rol, activo, creado_at) ON idcmd.usuarios TO idcmd_app;
GRANT INSERT (id, correo, nombre, rol, hash_clave, totp_cifrado, activo) ON idcmd.usuarios
    TO idcmd_app;
GRANT UPDATE (correo, nombre, rol, hash_clave, totp_cifrado, activo) ON idcmd.usuarios
    TO idcmd_app;

-- Ingesta: solo lo necesario para registrar estado.
GRANT SELECT (id, cliente_id, sitio_id, activo) ON idcmd.dispositivos TO idcmd_ingesta;
GRANT SELECT (id, sitio_id, revocado_at) ON idcmd.agentes TO idcmd_ingesta;
GRANT UPDATE (ultimo_latido_at, inicio_sesion, version, pendientes, desfase_reloj_s)
    ON idcmd.agentes TO idcmd_ingesta;
GRANT SELECT, INSERT ON idcmd.eventos_estado, idcmd.agregados_hora TO idcmd_ingesta;
GRANT SELECT, INSERT, UPDATE ON idcmd.estado_actual TO idcmd_ingesta;

-- RLS en TODAS las tablas del esquema (una prueba verifica que no falte ninguna). ------

ALTER TABLE idcmd.clientes ENABLE ROW LEVEL SECURITY;
ALTER TABLE idcmd.regionales ENABLE ROW LEVEL SECURITY;
ALTER TABLE idcmd.sitios ENABLE ROW LEVEL SECURITY;
ALTER TABLE idcmd.dispositivos ENABLE ROW LEVEL SECURITY;
ALTER TABLE idcmd.agentes ENABLE ROW LEVEL SECURITY;
ALTER TABLE idcmd.estado_actual ENABLE ROW LEVEL SECURITY;
ALTER TABLE idcmd.eventos_estado ENABLE ROW LEVEL SECURITY;
ALTER TABLE idcmd.agregados_hora ENABLE ROW LEVEL SECURITY;
ALTER TABLE idcmd.usuarios ENABLE ROW LEVEL SECURITY;
ALTER TABLE idcmd.alcances_usuario ENABLE ROW LEVEL SECURITY;
ALTER TABLE idcmd.asignaciones_tecnico ENABLE ROW LEVEL SECURITY;
ALTER TABLE idcmd.auditoria_accesos ENABLE ROW LEVEL SECURITY;

-- Portal: lectura según alcance.
CREATE POLICY lectura ON idcmd.clientes FOR SELECT TO idcmd_app
    USING (id IN (SELECT idcmd.clientes_visibles()));
CREATE POLICY lectura ON idcmd.regionales FOR SELECT TO idcmd_app
    USING (id IN (SELECT idcmd.regionales_visibles()));
CREATE POLICY lectura ON idcmd.sitios FOR SELECT TO idcmd_app
    USING (id IN (SELECT idcmd.sitios_visibles()));
CREATE POLICY lectura ON idcmd.dispositivos FOR SELECT TO idcmd_app
    USING (sitio_id IN (SELECT idcmd.sitios_visibles()));
CREATE POLICY lectura ON idcmd.agentes FOR SELECT TO idcmd_app
    USING (sitio_id IN (SELECT idcmd.sitios_visibles()));
CREATE POLICY lectura ON idcmd.estado_actual FOR SELECT TO idcmd_app
    USING (sitio_id IN (SELECT idcmd.sitios_visibles()));
CREATE POLICY lectura ON idcmd.eventos_estado FOR SELECT TO idcmd_app
    USING (sitio_id IN (SELECT idcmd.sitios_visibles()));
CREATE POLICY lectura ON idcmd.agregados_hora FOR SELECT TO idcmd_app
    USING (sitio_id IN (SELECT idcmd.sitios_visibles()));
CREATE POLICY lectura ON idcmd.usuarios FOR SELECT TO idcmd_app
    USING (idcmd.es_admin() OR id = idcmd.usuario_actual());
CREATE POLICY lectura ON idcmd.alcances_usuario FOR SELECT TO idcmd_app
    USING (idcmd.es_admin() OR usuario_id = idcmd.usuario_actual());
CREATE POLICY lectura ON idcmd.asignaciones_tecnico FOR SELECT TO idcmd_app
    USING (idcmd.es_admin() OR tecnico_id = idcmd.usuario_actual());
CREATE POLICY lectura ON idcmd.auditoria_accesos FOR SELECT TO idcmd_app
    USING (idcmd.es_admin());

-- Portal: solo el Admin escribe inventario, usuarios y asignaciones.
CREATE POLICY admin_escribe ON idcmd.clientes FOR ALL TO idcmd_app
    USING (idcmd.es_admin()) WITH CHECK (idcmd.es_admin());
CREATE POLICY admin_escribe ON idcmd.regionales FOR ALL TO idcmd_app
    USING (idcmd.es_admin()) WITH CHECK (idcmd.es_admin());
CREATE POLICY admin_escribe ON idcmd.sitios FOR ALL TO idcmd_app
    USING (idcmd.es_admin()) WITH CHECK (idcmd.es_admin());
CREATE POLICY admin_escribe ON idcmd.dispositivos FOR ALL TO idcmd_app
    USING (idcmd.es_admin()) WITH CHECK (idcmd.es_admin());
CREATE POLICY admin_escribe ON idcmd.agentes FOR ALL TO idcmd_app
    USING (idcmd.es_admin()) WITH CHECK (idcmd.es_admin());
CREATE POLICY admin_escribe ON idcmd.usuarios FOR ALL TO idcmd_app
    USING (idcmd.es_admin()) WITH CHECK (idcmd.es_admin());
CREATE POLICY admin_escribe ON idcmd.alcances_usuario FOR ALL TO idcmd_app
    USING (idcmd.es_admin()) WITH CHECK (idcmd.es_admin());
CREATE POLICY admin_escribe ON idcmd.asignaciones_tecnico FOR ALL TO idcmd_app
    USING (idcmd.es_admin()) WITH CHECK (idcmd.es_admin());

-- Auditoría: cualquiera registra sus propias acciones (o anónimas, p. ej. login fallido).
CREATE POLICY registrar ON idcmd.auditoria_accesos FOR INSERT TO idcmd_app
    WITH CHECK (usuario_id IS NULL OR usuario_id = idcmd.usuario_actual());

-- Ingesta: el agente solo ve y escribe su propio sitio.
CREATE POLICY agente_propio ON idcmd.agentes FOR SELECT TO idcmd_ingesta
    USING (id = idcmd.agente_actual());
CREATE POLICY agente_latido ON idcmd.agentes FOR UPDATE TO idcmd_ingesta
    USING (id = idcmd.agente_actual()) WITH CHECK (id = idcmd.agente_actual());
CREATE POLICY sitio_del_agente ON idcmd.dispositivos FOR SELECT TO idcmd_ingesta
    USING (sitio_id = idcmd.sitio_del_agente());
CREATE POLICY sitio_del_agente ON idcmd.eventos_estado FOR ALL TO idcmd_ingesta
    USING (sitio_id = idcmd.sitio_del_agente())
    WITH CHECK (sitio_id = idcmd.sitio_del_agente() AND agente_id = idcmd.agente_actual());
CREATE POLICY sitio_del_agente ON idcmd.agregados_hora FOR ALL TO idcmd_ingesta
    USING (sitio_id = idcmd.sitio_del_agente())
    WITH CHECK (sitio_id = idcmd.sitio_del_agente() AND agente_id = idcmd.agente_actual());
CREATE POLICY sitio_del_agente ON idcmd.estado_actual FOR ALL TO idcmd_ingesta
    USING (sitio_id = idcmd.sitio_del_agente())
    WITH CHECK (sitio_id = idcmd.sitio_del_agente());
