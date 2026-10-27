-- 0004 — Registro de operaciones administrativas de los equipos (ADR 0009).
--
-- Quién entró a un equipo (cuenta e IP de origen) y qué hizo. Es evidencia de auditoría:
-- solo se agrega; ni el portal ni la ingesta pueden modificarla ni borrarla.
-- Datos personales (Ley 1581): la cuenta y la IP pueden identificar a un operador; su
-- tratamiento debe constar en la política de datos y en el contrato con el cliente.

CREATE TABLE idcmd.operaciones_equipo (
    id                uuid PRIMARY KEY,   -- determinista en el agente: reenviar no duplica
    dispositivo_id    uuid NOT NULL,
    cliente_id        uuid NOT NULL,
    sitio_id          uuid NOT NULL,
    agente_id         uuid NOT NULL REFERENCES idcmd.agentes,
    ocurrido_at       timestamptz NOT NULL,
    tipo              text NOT NULL CHECK (tipo IN (
        'inicio_sesion', 'inicio_sesion_fallido', 'cierre_sesion', 'cambio_configuracion',
        'busqueda_video', 'reproduccion_video', 'exportacion_video', 'borrado_registros',
        'reinicio', 'actualizacion_firmware', 'otra')),
    cuenta            text,
    ip_origen         inet,
    codigo_fabricante text,
    recibido_at       timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (dispositivo_id, sitio_id, cliente_id)
        REFERENCES idcmd.dispositivos (id, sitio_id, cliente_id)
);
CREATE INDEX ON idcmd.operaciones_equipo (dispositivo_id, ocurrido_at);
CREATE INDEX ON idcmd.operaciones_equipo (sitio_id, ocurrido_at);

ALTER TABLE idcmd.operaciones_equipo ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON idcmd.operaciones_equipo TO idcmd_app;
GRANT SELECT, INSERT ON idcmd.operaciones_equipo TO idcmd_ingesta;

CREATE POLICY lectura ON idcmd.operaciones_equipo FOR SELECT TO idcmd_app
    USING (sitio_id IN (SELECT idcmd.sitios_visibles()));
CREATE POLICY sitio_del_agente ON idcmd.operaciones_equipo FOR ALL TO idcmd_ingesta
    USING (sitio_id = idcmd.sitio_del_agente())
    WITH CHECK (sitio_id = idcmd.sitio_del_agente() AND agente_id = idcmd.agente_actual());
