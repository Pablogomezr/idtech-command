#!/usr/bin/env bash
# Prueba de restauración (ADR 0002, una vez al mes): restaura un respaldo en una base de datos
# desechable, sin red, y muestra cuántos registros hay. No toca la base de producción.
#
#   sudo bash probar-restauracion.sh                         # el respaldo local más reciente
#   sudo bash probar-restauracion.sh bd-20261030T0617Z.dump.gpg   # una copia bajada del bucket
set -euo pipefail

BASE=${IDCMD_BASE:-/srv/idcmd}
archivo=${1:-$(ls -t "$BASE"/respaldos/bd-*.dump 2>/dev/null | head -1)}
[[ -n "$archivo" && -f "$archivo" ]] || { echo "No hay respaldo para probar."; exit 1; }
trabajo=$(mktemp -d)
nombre=idcmd-prueba-restauracion
trap 'docker rm -f "$nombre" >/dev/null 2>&1 || true; rm -rf "$trabajo"' EXIT

if [[ "$archivo" == *.gpg ]]; then
  gpg --batch --quiet --pinentry-mode loopback --passphrase-file "$BASE/respaldo.clave" \
      --decrypt --output "$trabajo/bd.dump" "$archivo"
else
  cp "$archivo" "$trabajo/bd.dump"
fi
chmod 644 "$trabajo/bd.dump"

docker run -d --name "$nombre" --network none -e POSTGRES_PASSWORD=desechable \
  -v "$trabajo:/respaldo:ro" postgres:16 >/dev/null
until docker exec "$nombre" pg_isready -U postgres -q; do sleep 1; done
sleep 2
# Los roles de la aplicación son globales (no van en pg_dump): se crean vacíos para los permisos.
docker exec "$nombre" psql -U postgres -q -c \
  "DO \$\$ BEGIN
     CREATE ROLE idcmd_app NOLOGIN; CREATE ROLE idcmd_ingesta NOLOGIN; CREATE ROLE idcmd_worker NOLOGIN;
   EXCEPTION WHEN duplicate_object THEN NULL; END \$\$;"
docker exec "$nombre" pg_restore -U postgres -d postgres --exit-on-error /respaldo/bd.dump

echo "Respaldo: $archivo"
docker exec "$nombre" psql -U postgres -tA -F ': ' -c "
  SELECT 'migraciones', count(*) FROM public.schema_migraciones UNION ALL
  SELECT 'sitios', count(*) FROM idcmd.sitios UNION ALL
  SELECT 'equipos', count(*) FROM idcmd.dispositivos UNION ALL
  SELECT 'usuarios', count(*) FROM idcmd.usuarios UNION ALL
  SELECT 'tickets', count(*) FROM idcmd.tickets UNION ALL
  SELECT 'eventos de estado', count(*) FROM idcmd.eventos_estado"
printf '\033[32m%s\033[0m\n' "Restauración correcta."
