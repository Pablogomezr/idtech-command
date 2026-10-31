#!/usr/bin/env bash
# Instala o actualiza la central de IDtech Command en este servidor (Ubuntu 24.04 con Docker).
# Se puede repetir: en una actualización conserva datos, usuarios, certificados y secretos.
#
#   sudo bash despliegue/produccion/instalar.sh
#
# Secretos: se generan aquí, al azar, en /srv/idcmd (solo root). Ninguna clave pasa por el
# código, el chat ni GitHub.
set -euo pipefail

BASE=${IDCMD_BASE:-/srv/idcmd}
ENTORNO=$BASE/produccion.env
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
COMPOSE=(docker compose --env-file "$ENTORNO" -f "$REPO/despliegue/produccion/docker-compose.yml")

rojo() { printf '\033[31m%s\033[0m\n' "$*"; }
amarillo() { printf '\033[33m%s\033[0m\n' "$*"; }
paso() { printf '\n\033[36m== %s ==\033[0m\n' "$*"; }
trap 'rojo "Falló en la línea $LINENO. Envíale a Claude el error de arriba (no contiene claves)."' ERR

[[ $EUID -eq 0 ]] || { rojo "Ejecútelo con sudo: sudo bash $0"; exit 1; }

paso "1/6 Docker"
# En Oracle, el primer arranque del servidor instala Docker (cloud-init): se espera a que acabe.
command -v cloud-init >/dev/null && cloud-init status --wait >/dev/null 2>&1 || true
if ! docker compose version >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2
  systemctl enable --now docker
fi
docker compose version

paso "2/6 Secretos del servidor"
mkdir -p "$BASE/sitios" "$BASE/respaldos"
chmod 700 "$BASE"
# La consola de la central corre como 10001 y deja aquí la configuración del agente demo.
install -d -o 10001 -g 10001 -m 700 "$BASE/sitios/demo"
if [[ ! -f "$ENTORNO" ]]; then
  read -r -p "Dominio del portal (p. ej. command.idtech.com.co): " DOMINIO
  read -r -p "Correo para los avisos de Let's Encrypt: " CORREO
  [[ "$DOMINIO" =~ ^[a-z0-9.-]+\.[a-z]{2,}$ ]] || { rojo "Dominio no válido."; exit 1; }
  (
    umask 077
    cat > "$ENTORNO" <<EOF
# Secretos de la central. Solo root. Copia de seguridad: en su gestor de claves, nunca en chat.
IDCMD_DOMINIO=$DOMINIO
IDCMD_ACME_CORREO=$CORREO
IDCMD_PG_CLAVE=$(openssl rand -hex 24)
IDCMD_CLAVE_SERVICIO=$(openssl rand -hex 24)
# Cifra los segundos factores: si se pierde, todos deben volver a registrar su app.
IDCMD_LLAVE_TOTP=$(openssl rand -base64 32)
# Respaldos fuera del servidor: enlace de SOLO ESCRITURA del bucket (termina en /o/). Ver guía.
IDCMD_RESPALDO_URL=
# Avisos por correo (opcional; sin esto quedan en el portal, menú Avisos)
IDCMD_SMTP_SERVIDOR=
IDCMD_SMTP_PUERTO=587
IDCMD_SMTP_USUARIO=
IDCMD_SMTP_CLAVE=
IDCMD_SMTP_REMITENTE=
# Avisos por Telegram (opcional): token del bot de @BotFather
IDCMD_TELEGRAM_TOKEN=
EOF
    # Frase que cifra los respaldos. Sin ella, un respaldo no se puede restaurar.
    openssl rand -base64 48 > "$BASE/respaldo.clave"
  )
  amarillo "Secretos creados en $ENTORNO y $BASE/respaldo.clave."
  amarillo "Al terminar, guarde una copia de ambos en su gestor de claves (vea la guía)."
fi
# shellcheck disable=SC1090
set -a; source "$ENTORNO"; set +a

paso "3/6 Dominio"
IP_SERVIDOR=$(curl -fsS --max-time 10 https://ifconfig.me || true)
IP_DNS=$(getent ahostsv4 "$IDCMD_DOMINIO" | awk 'NR==1{print $1}' || true)
if [[ -n "$IP_SERVIDOR" && "$IP_DNS" == "$IP_SERVIDOR" ]]; then
  echo "$IDCMD_DOMINIO -> $IP_DNS (este servidor)"
else
  amarillo "Ojo: $IDCMD_DOMINIO apunta a '${IP_DNS:-nada}' y este servidor es '${IP_SERVIDOR:-?}'."
  amarillo "Sin el registro DNS correcto no habrá certificado HTTPS (Caddy reintenta solo)."
fi

paso "4/6 Imágenes y base de datos (la primera vez tarda unos minutos)"
"${COMPOSE[@]}" build --pull
"${COMPOSE[@]}" up -d bd
"${COMPOSE[@]}" run --rm preparar

paso "5/6 Usuario administrador del portal"
if [[ ! -f "$BASE/.usuario-creado" ]]; then
  read -r -p "Correo para ingresar al portal: " CORREO_ADMIN
  read -r -p "Su nombre: " NOMBRE_ADMIN
  "${COMPOSE[@]}" run --rm preparar idcmd-central usuarios crear \
    --correo "$CORREO_ADMIN" --nombre "$NOMBRE_ADMIN" --rol admin
  touch "$BASE/.usuario-creado"
  amarillo "Agregue la clave de configuración a su app de autenticación AHORA."
  read -r -p "Presione Enter cuando la haya agregado. "
else
  echo "ya existe (si perdió el segundo factor: vea la guía, 'restablecer-mfa')"
fi

paso "6/6 Portal, servicio y respaldos"
"${COMPOSE[@]}" up -d --remove-orphans bd central worker caddy
install -m 755 "$REPO/despliegue/produccion/respaldar.sh" /usr/local/sbin/idcmd-respaldar
# Atajo a la consola de la central con esta versión:  sudo idcmd-central usuarios ...
printf '#!/bin/sh\nexec %s run --rm preparar idcmd-central "$@"\n' "${COMPOSE[*]}" \
  > /usr/local/sbin/idcmd-central
chmod 755 /usr/local/sbin/idcmd-central
printf '#!/bin/sh\nexec %s --profile demo up -d --build demo\n' "${COMPOSE[*]}" \
  > /usr/local/sbin/idcmd-demo-iniciar
chmod 755 /usr/local/sbin/idcmd-demo-iniciar
if [[ -f "$BASE/sitios/demo/agente/agente.yaml" ]]; then
  "${COMPOSE[@]}" --profile demo up -d --build demo   # al actualizar, la demo sigue viva
fi
install -m 644 "$REPO/despliegue/produccion/idcmd-respaldo.service" /etc/systemd/system/
install -m 644 "$REPO/despliegue/produccion/idcmd-respaldo.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now idcmd-respaldo.timer
"${COMPOSE[@]}" ps

echo
printf '\033[32m%s\033[0m\n' "Listo: https://$IDCMD_DOMINIO/portal"
echo "El certificado HTTPS puede tardar 1 o 2 minutos la primera vez."
[[ -n "${IDCMD_RESPALDO_URL:-}" ]] || amarillo "Respaldos: solo en este servidor hasta configurar IDCMD_RESPALDO_URL (vea la guía)."
