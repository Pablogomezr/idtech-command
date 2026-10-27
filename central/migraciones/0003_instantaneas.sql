-- 0003 — Instantáneas de salud: valor actual de las métricas y historial de cambios.
--
-- `metricas_actuales` guarda lo último que informó cada chequeo (retención, uso de disco,
-- usuarios registrados, modos de verificación...). `cambios_metricas` registra solo cambios
-- de métricas NO volátiles: es la auditoría de configuración del equipo (alguien agregó
-- usuarios al panel, se retiró un disco del NVR, cambió el firmware o el modo de una lectora).

CREATE TABLE idcmd.metricas_actuales (
    dispositivo_id uuid NOT NULL,
    chequeo        text NOT NULL,
    cliente_id     uuid NOT NULL,
    sitio_id       uuid NOT NULL,
    estado         idcmd.estado NOT NULL,
    metricas       jsonb NOT NULL,
    observado_at   timestamptz NOT NULL,
    recibido_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (dispositivo_id, chequeo),
    FOREIGN KEY (dispositivo_id, sitio_id, cliente_id)
        REFERENCES idcmd.dispositivos (id, sitio_id, cliente_id)
);
CREATE INDEX ON idcmd.metricas_actuales (sitio_id);

CREATE TABLE idcmd.cambios_metricas (
    id             uuid PRIMARY KEY,   -- el de la instantánea que trajo el cambio
    dispositivo_id uuid NOT NULL,
    chequeo        text NOT NULL,
    cliente_id     uuid NOT NULL,
    sitio_id       uuid NOT NULL,
    agente_id      uuid NOT NULL REFERENCES idcmd.agentes,
    observado_at   timestamptz NOT NULL,
    antes          jsonb,              -- solo las métricas que cambiaron
    despues        jsonb NOT NULL,
    recibido_at    timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (dispositivo_id, sitio_id, cliente_id)
        REFERENCES idcmd.dispositivos (id, sitio_id, cliente_id)
);
CREATE INDEX ON idcmd.cambios_metricas (dispositivo_id, observado_at);
CREATE INDEX ON idcmd.cambios_metricas (sitio_id, observado_at);

ALTER TABLE idcmd.metricas_actuales ENABLE ROW LEVEL SECURITY;
ALTER TABLE idcmd.cambios_metricas ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON idcmd.metricas_actuales, idcmd.cambios_metricas TO idcmd_app;
GRANT SELECT, INSERT, UPDATE ON idcmd.metricas_actuales TO idcmd_ingesta;
GRANT SELECT, INSERT ON idcmd.cambios_metricas TO idcmd_ingesta;

CREATE POLICY lectura ON idcmd.metricas_actuales FOR SELECT TO idcmd_app
    USING (sitio_id IN (SELECT idcmd.sitios_visibles()));
CREATE POLICY lectura ON idcmd.cambios_metricas FOR SELECT TO idcmd_app
    USING (sitio_id IN (SELECT idcmd.sitios_visibles()));

CREATE POLICY sitio_del_agente ON idcmd.metricas_actuales FOR ALL TO idcmd_ingesta
    USING (sitio_id = idcmd.sitio_del_agente())
    WITH CHECK (sitio_id = idcmd.sitio_del_agente());
CREATE POLICY sitio_del_agente ON idcmd.cambios_metricas FOR ALL TO idcmd_ingesta
    USING (sitio_id = idcmd.sitio_del_agente())
    WITH CHECK (sitio_id = idcmd.sitio_del_agente() AND agente_id = idcmd.agente_actual());
