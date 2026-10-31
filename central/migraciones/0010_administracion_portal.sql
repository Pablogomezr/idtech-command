-- Administración desde el portal: crear usuarios y restablecer su acceso sin la consola.
-- La clave se convierte en hash DENTRO de Postgres (como verificar_clave): la aplicación nunca
-- guarda ni lee hashes. Solo un administrador puede usar estas funciones.

CREATE FUNCTION idcmd.crear_usuario_portal(
    p_correo text, p_nombre text, p_rol idcmd.rol, p_clave text, p_totp bytea
) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, idcmd AS $$
DECLARE nuevo uuid;
BEGIN
    IF NOT idcmd.es_admin() THEN
        RAISE EXCEPTION 'solo un administrador crea usuarios' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF length(p_clave) < 12 THEN
        RAISE EXCEPTION 'la clave debe tener al menos 12 caracteres' USING ERRCODE = 'check_violation';
    END IF;
    INSERT INTO idcmd.usuarios (correo, nombre, rol, hash_clave, totp_cifrado)
    VALUES (p_correo, p_nombre, p_rol, crypt(p_clave, gen_salt('bf', 12)), p_totp)
    RETURNING id INTO nuevo;
    RETURN nuevo;
END $$;

-- Clave y segundo factor nuevos (celular perdido, clave olvidada): quita el bloqueo por intentos
-- y cierra las sesiones abiertas de ese usuario.
CREATE FUNCTION idcmd.restablecer_acceso(p_usuario uuid, p_clave text, p_totp bytea)
RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, idcmd AS $$
BEGIN
    IF NOT idcmd.es_admin() THEN
        RAISE EXCEPTION 'solo un administrador restablece accesos' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF length(p_clave) < 12 THEN
        RAISE EXCEPTION 'la clave debe tener al menos 12 caracteres' USING ERRCODE = 'check_violation';
    END IF;
    UPDATE idcmd.usuarios
       SET hash_clave = crypt(p_clave, gen_salt('bf', 12)), totp_cifrado = p_totp,
           intentos_fallidos = 0, bloqueado_hasta = NULL
     WHERE id = p_usuario;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no existe el usuario' USING ERRCODE = 'no_data_found';
    END IF;
    UPDATE idcmd.sesiones SET cerrada_at = now() WHERE usuario_id = p_usuario AND cerrada_at IS NULL;
END $$;

-- Desactivar a alguien (se fue de la empresa) debe sacarlo ya, no cuando venza su sesión.
CREATE FUNCTION idcmd.cambiar_activo(p_usuario uuid, p_activo boolean)
RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, idcmd AS $$
BEGIN
    IF NOT idcmd.es_admin() THEN
        RAISE EXCEPTION 'solo un administrador' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF p_usuario = idcmd.usuario_actual() AND NOT p_activo THEN
        RAISE EXCEPTION 'no puede desactivarse a sí mismo' USING ERRCODE = 'check_violation';
    END IF;
    UPDATE idcmd.usuarios SET activo = p_activo WHERE id = p_usuario;
    IF NOT p_activo THEN
        UPDATE idcmd.sesiones SET cerrada_at = now()
         WHERE usuario_id = p_usuario AND cerrada_at IS NULL;
    END IF;
END $$;

REVOKE EXECUTE ON FUNCTION idcmd.crear_usuario_portal(text, text, idcmd.rol, text, bytea),
    idcmd.restablecer_acceso(uuid, text, bytea), idcmd.cambiar_activo(uuid, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION idcmd.crear_usuario_portal(text, text, idcmd.rol, text, bytea),
    idcmd.restablecer_acceso(uuid, text, bytea), idcmd.cambiar_activo(uuid, boolean) TO idcmd_app;
