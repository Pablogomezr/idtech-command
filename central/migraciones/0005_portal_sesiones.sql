-- 0005 — Portal: verificación de claves en la base, bloqueo por intentos y sesiones.
--
-- Regla del esquema: el rol del portal (`idcmd_app`) puede ESCRIBIR hashes de claves pero
-- nunca LEERLOS. Por eso la clave se verifica aquí dentro (pgcrypto, bcrypt) y la aplicación
-- solo recibe "sí, es el usuario X" o nada. Lo mismo con las sesiones: la cookie lleva un
-- token aleatorio; la base guarda su SHA-256 y solo responde a quién pertenece.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE idcmd.usuarios
    ADD COLUMN intentos_fallidos integer NOT NULL DEFAULT 0,
    ADD COLUMN bloqueado_hasta   timestamptz;
COMMENT ON COLUMN idcmd.usuarios.hash_clave IS
    'bcrypt (pgcrypto) de la clave del portal; se verifica solo con idcmd.verificar_clave';
COMMENT ON COLUMN idcmd.usuarios.totp_cifrado IS
    'secreto TOTP cifrado por la aplicación (AES-256-GCM, llave fuera de la base)';

CREATE TABLE idcmd.sesiones (
    token_hash  bytea PRIMARY KEY CHECK (octet_length(token_hash) = 32),
    usuario_id  uuid NOT NULL REFERENCES idcmd.usuarios ON DELETE CASCADE,
    creada_at   timestamptz NOT NULL DEFAULT now(),
    expira_at   timestamptz NOT NULL,
    ip          inet,
    cerrada_at  timestamptz
);
CREATE INDEX ON idcmd.sesiones (usuario_id);

-- Nadie del portal lee ni escribe sesiones directamente: solo por las funciones de abajo.
ALTER TABLE idcmd.sesiones ENABLE ROW LEVEL SECURITY;

-- Intentos fallidos: 5 seguidos bloquean la cuenta 15 minutos. Devuelve el usuario y su
-- secreto TOTP cifrado solo si la clave es correcta, la cuenta está activa y no bloqueada.
CREATE FUNCTION idcmd.verificar_clave(p_correo citext, p_clave text)
RETURNS TABLE (usuario_id uuid, totp_cifrado bytea)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, idcmd AS $$
DECLARE
    u idcmd.usuarios%ROWTYPE;
BEGIN
    SELECT * INTO u FROM idcmd.usuarios WHERE correo = p_correo FOR UPDATE;
    IF NOT FOUND OR NOT u.activo OR u.hash_clave IS NULL THEN
        -- Mismo costo que una verificación real: no revela si el correo existe.
        PERFORM crypt(p_clave, gen_salt('bf', 10));
        RETURN;
    END IF;
    IF u.bloqueado_hasta IS NOT NULL AND u.bloqueado_hasta > now() THEN
        RETURN;
    END IF;
    IF crypt(p_clave, u.hash_clave) = u.hash_clave THEN
        -- El contador NO se reinicia aquí: con la clave robada se podrían probar códigos
        -- TOTP sin fin. Se reinicia solo con clave Y código correctos (registrar_ingreso).
        usuario_id := u.id;
        totp_cifrado := u.totp_cifrado;
        RETURN NEXT;
    ELSE
        UPDATE idcmd.usuarios
           SET intentos_fallidos = intentos_fallidos + 1,
               bloqueado_hasta = CASE WHEN intentos_fallidos + 1 >= 5
                                      THEN now() + interval '15 minutes' END
         WHERE id = u.id;
    END IF;
END $$;

-- Un código TOTP equivocado también cuenta como intento fallido.
CREATE FUNCTION idcmd.registrar_fallo_mfa(p_usuario uuid) RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, idcmd AS $$
    UPDATE idcmd.usuarios
       SET intentos_fallidos = intentos_fallidos + 1,
           bloqueado_hasta = CASE WHEN intentos_fallidos + 1 >= 5
                                  THEN now() + interval '15 minutes' END
     WHERE id = p_usuario
$$;

CREATE FUNCTION idcmd.registrar_ingreso(p_usuario uuid) RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, idcmd AS $$
    UPDATE idcmd.usuarios SET intentos_fallidos = 0, bloqueado_hasta = NULL WHERE id = p_usuario
$$;

CREATE FUNCTION idcmd.crear_sesion(
    p_token_hash bytea, p_usuario uuid, p_duracion interval, p_ip inet
) RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, idcmd AS $$
    INSERT INTO idcmd.sesiones (token_hash, usuario_id, expira_at, ip)
    VALUES (p_token_hash, p_usuario, now() + p_duracion, p_ip)
$$;

-- Usuario dueño de una sesión vigente (y activo), o NULL.
CREATE FUNCTION idcmd.usuario_de_sesion(p_token_hash bytea) RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, idcmd AS $$
    SELECT s.usuario_id
      FROM idcmd.sesiones s JOIN idcmd.usuarios u ON u.id = s.usuario_id
     WHERE s.token_hash = p_token_hash
       AND s.cerrada_at IS NULL
       AND s.expira_at > now()
       AND u.activo
$$;

CREATE FUNCTION idcmd.cerrar_sesion(p_token_hash bytea) RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, idcmd AS $$
    UPDATE idcmd.sesiones SET cerrada_at = now()
     WHERE token_hash = p_token_hash AND cerrada_at IS NULL
$$;

REVOKE EXECUTE ON FUNCTION
    idcmd.verificar_clave(citext, text),
    idcmd.registrar_fallo_mfa(uuid),
    idcmd.registrar_ingreso(uuid),
    idcmd.crear_sesion(bytea, uuid, interval, inet),
    idcmd.usuario_de_sesion(bytea),
    idcmd.cerrar_sesion(bytea)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    idcmd.verificar_clave(citext, text),
    idcmd.registrar_fallo_mfa(uuid),
    idcmd.registrar_ingreso(uuid),
    idcmd.crear_sesion(bytea, uuid, interval, inet),
    idcmd.usuario_de_sesion(bytea),
    idcmd.cerrar_sesion(bytea)
TO idcmd_app;

-- Las columnas nuevas de usuarios tampoco las lee el portal.
REVOKE SELECT ON idcmd.usuarios FROM idcmd_app;
GRANT SELECT (id, correo, nombre, rol, activo, creado_at) ON idcmd.usuarios TO idcmd_app;

-- operaciones_equipo (0004) ya es legible por el portal según el sitio (RLS).
