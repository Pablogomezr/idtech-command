#!/usr/bin/env bash
# Respaldo de la central (ADR 0002): base de datos cada 6 h y fotos/firmas una vez al día.
# Copia local en /srv/idcmd/respaldos (7 días) y, si IDCMD_RESPALDO_URL está definido, copia
# cifrada (GPG AES-256) en el bucket de Bogotá mediante un enlace de solo escritura: aunque
# alguien tomara el servidor, no podría leer ni borrar las copias externas.
#
# Lo ejecuta systemd (idcmd-respaldo.timer). A mano: sudo idcmd-respaldar
set -euo pipefail

BASE=${IDCMD_BASE:-/srv/idcmd}
DESTINO=$BASE/respaldos
FRASE=$BASE/respaldo.clave
CONTENEDOR_BD=${IDCMD_CONTENEDOR_BD:-idcmd-bd-1}
VOLUMEN_ARCHIVOS=${IDCMD_VOLUMEN_ARCHIVOS:-idcmd_archivos}
IMAGEN=${IDCMD_IMAGEN:-idcmd-central:prod}
DIAS_LOCALES=7

# shellcheck disable=SC1091
set -a; source "$BASE/produccion.env"; set +a
umask 077
mkdir -p "$DESTINO"
chmod 700 "$DESTINO"
sello=$(date -u +%Y%m%dT%H%MZ)
fallo=0  # una subida fallida no impide el resto, pero el servicio queda en error

subir() {  # subir <archivo>: cifra, sube con tamaño conocido y borra la copia cifrada
  [[ -n "${IDCMD_RESPALDO_URL:-}" ]] || return 0
  local nombre cifrado
  nombre="$(hostname)/$(basename "$1").gpg"
  cifrado="$1.gpg"
  gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$FRASE" \
      --symmetric --cipher-algo AES256 --output "$cifrado" "$1"
  if curl -fsS --retry 3 --max-time 1800 -T "$cifrado" \
       -H "Content-Type: application/octet-stream" "${IDCMD_RESPALDO_URL%/}/$nombre" >/dev/null; then
    echo "subido: $nombre"
  else
    echo "ERROR: no se pudo subir $nombre (la copia local queda)" >&2
    rm -f "$cifrado"
    return 1
  fi
  rm -f "$cifrado"
}

# Base de datos: formato custom (pg_restore), consistente aunque la central esté escribiendo.
bd="$DESTINO/bd-$sello.dump"
docker exec "$CONTENEDOR_BD" pg_dump -U postgres -Fc postgres > "$bd.parcial"
mv "$bd.parcial" "$bd"
echo "base de datos: $bd ($(du -h "$bd" | cut -f1))"
subir "$bd" || fallo=1

# Fotos y firmas: una vez al día (son muchas y casi no cambian).
hoy=$(date -u +%Y%m%d)
if ! ls "$DESTINO"/archivos-"$hoy"T*.tar.gz >/dev/null 2>&1; then
  arch="$DESTINO/archivos-$sello.tar.gz"
  docker run --rm --network none -v "$VOLUMEN_ARCHIVOS":/a:ro --entrypoint tar "$IMAGEN" \
    czf - -C /a . > "$arch.parcial"
  mv "$arch.parcial" "$arch"
  echo "fotos y firmas: $arch ($(du -h "$arch" | cut -f1))"
  subir "$arch" || fallo=1
fi

find "$DESTINO" -maxdepth 1 -type f \( -name 'bd-*.dump' -o -name 'archivos-*.tar.gz' \) \
  -mtime +"$DIAS_LOCALES" -delete
find "$DESTINO" -maxdepth 1 \( -name '*.parcial' -o -name '*.gpg' \) -mmin +120 -delete
exit "$fallo"
