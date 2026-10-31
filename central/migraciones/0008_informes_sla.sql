-- Informe mensual de SLA (ADR 0004): exclusiones del cálculo, informes emitidos y su envío.

-- Meta de disponibilidad del contrato y si el informe viaja como PDF adjunto (supuesto P7: por
-- defecto solo el enlace).
ALTER TABLE idcmd.clientes
    ADD COLUMN meta_disponibilidad numeric(5, 2) NOT NULL DEFAULT 99.50
        CHECK (meta_disponibilidad > 0 AND meta_disponibilidad <= 100),
    ADD COLUMN informe_pdf_adjunto boolean NOT NULL DEFAULT false;

-- Tiempo que no cuenta en la disponibilidad: mantenimiento acordado o causa del cliente
-- (supuesto P3). Sin dispositivo, aplica a todo el sitio. Se registra, no se edita en silencio:
-- quién la creó queda guardado y el informe la lista.
CREATE TABLE idcmd.exclusiones_sla (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cliente_id     uuid NOT NULL,
    sitio_id       uuid NOT NULL,
    dispositivo_id uuid,
    motivo         text NOT NULL CHECK (motivo IN ('mantenimiento', 'causa_cliente')),
    desde          timestamptz NOT NULL,
    hasta          timestamptz NOT NULL CHECK (hasta > desde),
    descripcion    text NOT NULL CHECK (length(descripcion) BETWEEN 3 AND 300),
    creado_por     uuid REFERENCES idcmd.usuarios,
    creado_at      timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (sitio_id, cliente_id) REFERENCES idcmd.sitios (id, cliente_id),
    FOREIGN KEY (dispositivo_id, sitio_id, cliente_id)
        REFERENCES idcmd.dispositivos (id, sitio_id, cliente_id)
);
CREATE INDEX ON idcmd.exclusiones_sla (sitio_id, desde);

-- Informe emitido al cerrar el mes: es lo que recibió el cliente y lo que se compara mes a mes
-- (los agregados por hora solo se conservan 90 días). Recalcularlo es explícito (consola).
CREATE TABLE idcmd.informes_mensuales (
    cliente_id     uuid NOT NULL,
    sitio_id       uuid NOT NULL,
    periodo        date NOT NULL CHECK (extract(day FROM periodo) = 1),
    disponibilidad numeric(7, 4),          -- NULL: sin tiempo medible en el mes
    resumen        jsonb NOT NULL,
    generado_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (sitio_id, periodo),
    FOREIGN KEY (sitio_id, cliente_id) REFERENCES idcmd.sitios (id, cliente_id)
);

-- Un aviso puede llevar el informe en PDF; se genera al enviar, desde el informe emitido.
ALTER TABLE idcmd.notificaciones ADD COLUMN adjunto text
    CHECK (adjunto IS NULL OR adjunto ~ '^informe:[0-9a-f-]{36}:[0-9]{4}-[0-9]{2}$');

ALTER TABLE idcmd.exclusiones_sla ENABLE ROW LEVEL SECURITY;
ALTER TABLE idcmd.informes_mensuales ENABLE ROW LEVEL SECURITY;

-- Portal: cada quien ve lo de sus sitios; solo el admin registra exclusiones.
GRANT SELECT ON idcmd.exclusiones_sla, idcmd.informes_mensuales TO idcmd_app;
GRANT INSERT, DELETE ON idcmd.exclusiones_sla TO idcmd_app;
CREATE POLICY lectura ON idcmd.exclusiones_sla FOR SELECT TO idcmd_app
    USING (sitio_id IN (SELECT idcmd.sitios_visibles()));
CREATE POLICY admin_escribe ON idcmd.exclusiones_sla FOR ALL TO idcmd_app
    USING (idcmd.es_admin()) WITH CHECK (idcmd.es_admin());
CREATE POLICY lectura ON idcmd.informes_mensuales FOR SELECT TO idcmd_app
    USING (sitio_id IN (SELECT idcmd.sitios_visibles()));

-- Worker: calcula y emite el informe del mes cerrado.
GRANT SELECT ON idcmd.eventos_estado, idcmd.agregados_hora, idcmd.exclusiones_sla,
    idcmd.cambios_metricas, idcmd.regionales TO idcmd_worker;
GRANT SELECT, INSERT, UPDATE ON idcmd.informes_mensuales TO idcmd_worker;
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['eventos_estado', 'agregados_hora', 'exclusiones_sla',
        'cambios_metricas', 'regionales', 'informes_mensuales']
    LOOP
        EXECUTE format('CREATE POLICY worker ON idcmd.%I FOR ALL TO idcmd_worker'
                       ' USING (true) WITH CHECK (true)', t);
    END LOOP;
END $$;

-- Enlace del informe para el cliente (sin sesión, firmado como el de seguimiento): entrega solo
-- el informe ya emitido, nunca un cálculo en vivo.
CREATE FUNCTION idcmd.informe_emitido(p_sitio uuid, p_periodo date)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, idcmd AS $$
    SELECT resumen FROM idcmd.informes_mensuales WHERE sitio_id = p_sitio AND periodo = p_periodo
$$;
REVOKE EXECUTE ON FUNCTION idcmd.informe_emitido(uuid, date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION idcmd.informe_emitido(uuid, date) TO idcmd_app;
