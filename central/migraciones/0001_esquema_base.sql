-- 0001 — Esquema base: jerarquía, inventario, agentes, estado, historial, usuarios y auditoría.
--
-- Reglas del modelo:
-- * Toda tabla con datos de un sitio lleva `cliente_id` y `sitio_id` desnormalizados: la RLS
--   filtra por ellos sin joins. Llaves foráneas compuestas garantizan que sean coherentes.
-- * Ninguna columna guarda credenciales de equipos (ver tests/test_sin_credenciales.py).
--   `credencial_ref` es solo el alias que el agente resuelve en su bóveda local.

CREATE SCHEMA idcmd;
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TYPE idcmd.estado AS ENUM ('ok', 'degradado', 'caido', 'desconocido');
CREATE TYPE idcmd.rol AS ENUM ('admin', 'tecnico', 'cliente');
CREATE TYPE idcmd.tipo_equipo AS ENUM (
    'camara', 'nvr', 'dvr', 'servidor_vms', 'lectora', 'controladora', 'switch', 'ups', 'otro'
);
CREATE TYPE idcmd.tipo_asignacion AS ENUM ('principal', 'suplente');

-- Jerarquía: Cliente -> Regional -> Sitio -> Dispositivo ------------------------------

CREATE TABLE idcmd.clientes (
    id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    nombre    text NOT NULL,
    nit       text,
    creado_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE idcmd.regionales (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cliente_id uuid NOT NULL REFERENCES idcmd.clientes,
    nombre     text NOT NULL,
    UNIQUE (cliente_id, nombre),
    UNIQUE (id, cliente_id)
);

CREATE TABLE idcmd.sitios (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cliente_id      uuid NOT NULL,
    regional_id     uuid NOT NULL,
    nombre          text NOT NULL,
    ciudad          text,
    direccion       text,
    lat             numeric(9, 6),
    lon             numeric(9, 6),
    radio_checkin_m integer NOT NULL DEFAULT 200 CHECK (radio_checkin_m > 0),
    zona_horaria    text NOT NULL DEFAULT 'America/Bogota',
    creado_at       timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (regional_id, cliente_id) REFERENCES idcmd.regionales (id, cliente_id),
    UNIQUE (id, cliente_id)
);

CREATE TABLE idcmd.dispositivos (
    id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cliente_id                uuid NOT NULL,
    sitio_id                  uuid NOT NULL,
    nombre                    text NOT NULL,
    tipo_equipo               idcmd.tipo_equipo NOT NULL,
    marca                     text,
    modelo                    text,
    direccion                 text,          -- IP o nombre interno; nunca usuario ni clave
    ubicacion                 text,
    fecha_instalacion         date,
    retencion_contratada_dias integer CHECK (retencion_contratada_dias > 0),
    criticidad                smallint NOT NULL DEFAULT 2 CHECK (criticidad BETWEEN 1 AND 3),
    credencial_ref            text CHECK (credencial_ref ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
    activo                    boolean NOT NULL DEFAULT true,
    creado_at                 timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (sitio_id, cliente_id) REFERENCES idcmd.sitios (id, cliente_id),
    UNIQUE (id, sitio_id, cliente_id)
);
CREATE INDEX ON idcmd.dispositivos (sitio_id);

-- Agentes en sitio ------------------------------------------------------------------

CREATE TABLE idcmd.agentes (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cliente_id       uuid NOT NULL,
    sitio_id         uuid NOT NULL,
    nombre           text NOT NULL,
    -- SHA-256 del token; el token solo se muestra una vez al crearlo.
    token_hash       bytea NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
    creado_at        timestamptz NOT NULL DEFAULT now(),
    revocado_at      timestamptz,
    ultimo_latido_at timestamptz,
    inicio_sesion    timestamptz,
    version          text,
    pendientes       integer,
    desfase_reloj_s  double precision,
    FOREIGN KEY (sitio_id, cliente_id) REFERENCES idcmd.sitios (id, cliente_id),
    UNIQUE (id, sitio_id, cliente_id)
);
CREATE INDEX ON idcmd.agentes (sitio_id);

-- Estado y su historial (se guardan cambios y agregados, no sondeos crudos) -----------

CREATE TABLE idcmd.estado_actual (
    dispositivo_id uuid NOT NULL,
    chequeo        text NOT NULL,
    cliente_id     uuid NOT NULL,
    sitio_id       uuid NOT NULL,
    estado         idcmd.estado NOT NULL,
    desde          timestamptz NOT NULL,
    codigo         text,
    metricas       jsonb NOT NULL DEFAULT '{}',
    actualizado_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (dispositivo_id, chequeo),
    FOREIGN KEY (dispositivo_id, sitio_id, cliente_id)
        REFERENCES idcmd.dispositivos (id, sitio_id, cliente_id)
);
CREATE INDEX ON idcmd.estado_actual (sitio_id);

CREATE TABLE idcmd.eventos_estado (
    id              uuid PRIMARY KEY,       -- generado por el agente: reenviar es idempotente
    dispositivo_id  uuid NOT NULL,
    cliente_id      uuid NOT NULL,
    sitio_id        uuid NOT NULL,
    agente_id       uuid NOT NULL REFERENCES idcmd.agentes,
    chequeo         text NOT NULL,
    estado_anterior idcmd.estado NOT NULL,
    estado_nuevo    idcmd.estado NOT NULL,
    observado_at    timestamptz NOT NULL,   -- hora del agente: inicio real del cambio
    recibido_at     timestamptz NOT NULL DEFAULT now(),
    codigo          text,
    metricas        jsonb NOT NULL DEFAULT '{}',
    FOREIGN KEY (dispositivo_id, sitio_id, cliente_id)
        REFERENCES idcmd.dispositivos (id, sitio_id, cliente_id)
);
CREATE INDEX ON idcmd.eventos_estado (dispositivo_id, observado_at);
CREATE INDEX ON idcmd.eventos_estado (sitio_id, observado_at);

-- Una misma hora puede llegar en varias partes (una por sesión del agente); se suman.
-- Volumen estimado con 2.000 equipos: ~17 M filas/año. Se conservan 90 días (trabajo de
-- retención en el worker); los indicadores de largo plazo se calculan desde eventos.
CREATE TABLE idcmd.agregados_hora (
    id                   uuid PRIMARY KEY,
    dispositivo_id       uuid NOT NULL,
    cliente_id           uuid NOT NULL,
    sitio_id             uuid NOT NULL,
    agente_id            uuid NOT NULL REFERENCES idcmd.agentes,
    chequeo              text NOT NULL,
    hora                 timestamptz NOT NULL,
    muestras_ok          integer NOT NULL CHECK (muestras_ok >= 0),
    muestras_degradado   integer NOT NULL CHECK (muestras_degradado >= 0),
    muestras_caido       integer NOT NULL CHECK (muestras_caido >= 0),
    muestras_desconocido integer NOT NULL CHECK (muestras_desconocido >= 0),
    latencia_prom_ms     real,
    latencia_max_ms      real,
    recibido_at          timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (dispositivo_id, sitio_id, cliente_id)
        REFERENCES idcmd.dispositivos (id, sitio_id, cliente_id)
);
CREATE INDEX ON idcmd.agregados_hora (dispositivo_id, chequeo, hora);
CREATE INDEX ON idcmd.agregados_hora (recibido_at);

-- Usuarios del portal y su alcance ----------------------------------------------------

CREATE TABLE idcmd.usuarios (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    correo       citext NOT NULL UNIQUE,
    nombre       text NOT NULL,
    rol          idcmd.rol NOT NULL,
    hash_clave   text,           -- argon2id de la clave del portal (no es credencial de equipo)
    totp_cifrado bytea,          -- secreto TOTP cifrado por la aplicación
    activo       boolean NOT NULL DEFAULT true,
    creado_at    timestamptz NOT NULL DEFAULT now()
);

-- Alcance de un usuario Cliente: todo un cliente, una regional o un sitio.
CREATE TABLE idcmd.alcances_usuario (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    usuario_id  uuid NOT NULL REFERENCES idcmd.usuarios ON DELETE CASCADE,
    cliente_id  uuid NOT NULL REFERENCES idcmd.clientes,
    regional_id uuid,
    sitio_id    uuid,
    CHECK (regional_id IS NULL OR sitio_id IS NULL),
    FOREIGN KEY (regional_id, cliente_id) REFERENCES idcmd.regionales (id, cliente_id),
    FOREIGN KEY (sitio_id, cliente_id) REFERENCES idcmd.sitios (id, cliente_id)
);
CREATE INDEX ON idcmd.alcances_usuario (usuario_id);

-- Asignación de sitios a técnicos: ven y reciben alertas SOLO de sus sitios.
CREATE TABLE idcmd.asignaciones_tecnico (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tecnico_id    uuid NOT NULL REFERENCES idcmd.usuarios,
    cliente_id    uuid NOT NULL,
    sitio_id      uuid NOT NULL,
    tipo          idcmd.tipo_asignacion NOT NULL DEFAULT 'principal',
    vigente_desde timestamptz NOT NULL DEFAULT now(),
    vigente_hasta timestamptz,
    asignado_por  uuid REFERENCES idcmd.usuarios,
    creado_at     timestamptz NOT NULL DEFAULT now(),
    CHECK (vigente_hasta IS NULL OR vigente_hasta > vigente_desde),
    FOREIGN KEY (sitio_id, cliente_id) REFERENCES idcmd.sitios (id, cliente_id)
);
CREATE INDEX ON idcmd.asignaciones_tecnico (tecnico_id);
CREATE UNIQUE INDEX un_principal_vigente_por_sitio
    ON idcmd.asignaciones_tecnico (sitio_id) WHERE tipo = 'principal' AND vigente_hasta IS NULL;

-- El rol se valida en la base para que ningún error de la aplicación lo salte.
CREATE FUNCTION idcmd.validar_asignacion_tecnico() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, idcmd AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM idcmd.usuarios WHERE id = NEW.tecnico_id AND rol = 'tecnico') THEN
        RAISE EXCEPTION 'solo se asignan sitios a usuarios con rol tecnico';
    END IF;
    RETURN NEW;
END $$;

CREATE FUNCTION idcmd.validar_alcance_cliente() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, idcmd AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM idcmd.usuarios WHERE id = NEW.usuario_id AND rol = 'cliente') THEN
        RAISE EXCEPTION 'solo los usuarios con rol cliente tienen alcance por cliente/regional/sitio';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER solo_tecnicos BEFORE INSERT OR UPDATE ON idcmd.asignaciones_tecnico
    FOR EACH ROW EXECUTE FUNCTION idcmd.validar_asignacion_tecnico();
CREATE TRIGGER solo_clientes BEFORE INSERT OR UPDATE ON idcmd.alcances_usuario
    FOR EACH ROW EXECUTE FUNCTION idcmd.validar_alcance_cliente();

-- Auditoría de accesos al portal: solo se agrega, nunca se modifica ni se borra. --------

CREATE TABLE idcmd.auditoria_accesos (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    usuario_id uuid REFERENCES idcmd.usuarios,
    accion     text NOT NULL,
    recurso    text,
    detalle    jsonb NOT NULL DEFAULT '{}',
    ip         inet,
    at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON idcmd.auditoria_accesos (usuario_id, at);
