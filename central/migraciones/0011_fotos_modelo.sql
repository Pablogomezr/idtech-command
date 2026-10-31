-- Foto de cada modelo de equipo (por referencia, p. ej. DS-2CD2T47G2-L), para el tablero.
-- No son datos de un sitio ni de personas: la ve cualquier usuario del portal; solo el
-- administrador la cambia. La imagen ya llega reducida (~30-80 KB) y sin metadatos: va en la
-- base para que viaje con los respaldos cifrados y se quede en Colombia como todo lo demás.

CREATE TABLE idcmd.fotos_modelo (
    modelo     text PRIMARY KEY CHECK (modelo = upper(modelo) AND modelo !~ '\s'
                                       AND length(modelo) BETWEEN 1 AND 80),
    mime       text NOT NULL CHECK (mime IN ('image/webp', 'image/png', 'image/jpeg')),
    contenido  bytea NOT NULL CHECK (length(contenido) <= 512 * 1024),
    sha256     bytea NOT NULL,
    subido_por uuid REFERENCES idcmd.usuarios,
    subido_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE idcmd.fotos_modelo ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON idcmd.fotos_modelo TO idcmd_app;
CREATE POLICY lectura ON idcmd.fotos_modelo FOR SELECT TO idcmd_app
    USING (idcmd.usuario_actual() IS NOT NULL);
CREATE POLICY admin_escribe ON idcmd.fotos_modelo FOR ALL TO idcmd_app
    USING (idcmd.es_admin()) WITH CHECK (idcmd.es_admin());
