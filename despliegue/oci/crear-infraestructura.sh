#!/usr/bin/env bash
# Crea en Oracle Cloud (Bogotá) todo lo que necesita la central de IDtech Command:
# compartimento, red, firewall, servidor ARM, IP pública reservada y bucket de respaldos.
#
# Se ejecuta en Cloud Shell (consola de Oracle, ícono >_ arriba a la derecha), que ya trae la
# CLI de OCI autenticada con tu usuario: este script no pide ni guarda ninguna clave.
#   1. Cloud Shell -> menú del engranaje -> Upload -> este archivo
#   2. bash crear-infraestructura.sh
# Se puede repetir: lo que ya existe se reutiliza.
set -euo pipefail

COMPARTIMENTO=idcmd
FORMA=VM.Standard.A1.Flex
OCPUS=2
MEMORIA_GB=12
DISCO_GB=100

rojo() { printf '\033[31m%s\033[0m\n' "$*"; }
paso() { printf '\n\033[36m== %s ==\033[0m\n' "$*"; }
# Primer id de una lista de la CLI (vacío si no hay ninguno).
primero() { jq -r '.data[0].id // empty'; }

if [[ "${OCI_REGION:-}" != "sa-bogota-1" ]]; then
  rojo "Cloud Shell está en la región '${OCI_REGION:-?}'. Cámbiala a Colombia Central (Bogota)"
  rojo "en el selector de región de la consola y vuelve a abrir Cloud Shell."
  exit 1
fi
TENANCY="${OCI_TENANCY:?Cloud Shell no definió OCI_TENANCY}"

paso "Datos que necesito (ninguno es secreto)"
echo "Llave PÚBLICA SSH: en tu PC, PowerShell ->  Get-Content \$HOME\\.ssh\\idcmd_oci.pub"
read -r -p "Pega la llave pública (empieza por ssh-ed25519): " LLAVE
[[ "$LLAVE" == ssh-ed25519\ * || "$LLAVE" == ssh-rsa\ * ]] || { rojo "Eso no parece una llave pública."; exit 1; }
echo "IP pública de la oficina: abre https://ifconfig.me desde un PC de la oficina."
read -r -p "IP pública de la oficina: " IP_OFICINA
[[ "$IP_OFICINA" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || { rojo "IP no válida."; exit 1; }
LLAVE_ARCHIVO=$(mktemp)
echo "$LLAVE" > "$LLAVE_ARCHIVO"

paso "1/7 Compartimento '$COMPARTIMENTO'"
C=$(oci iam compartment list -c "$TENANCY" --name "$COMPARTIMENTO" --lifecycle-state ACTIVE --all | primero)
if [[ -z "$C" ]]; then
  C=$(oci iam compartment create -c "$TENANCY" --name "$COMPARTIMENTO" \
        --description "IDtech Command produccion" --wait-for-state ACTIVE | jq -r .data.id)
  echo "creado; espero a que se propague..."
  sleep 30
fi
echo "$C"

paso "2/7 Red 'idcmd-red'"
VCN=$(oci network vcn list -c "$C" --display-name idcmd-red --lifecycle-state AVAILABLE --all | primero)
if [[ -z "$VCN" ]]; then
  VCN=$(oci network vcn create -c "$C" --display-name idcmd-red --dns-label idcmdred \
          --cidr-blocks '["10.0.0.0/16"]' --wait-for-state AVAILABLE | jq -r .data.id)
fi
IGW=$(oci network internet-gateway list -c "$C" --vcn-id "$VCN" --all | primero)
if [[ -z "$IGW" ]]; then
  IGW=$(oci network internet-gateway create -c "$C" --vcn-id "$VCN" --is-enabled true \
          --display-name idcmd-salida --wait-for-state AVAILABLE | jq -r .data.id)
fi
RT=$(oci network vcn get --vcn-id "$VCN" | jq -r '.data."default-route-table-id"')
oci network route-table update --rt-id "$RT" --force --route-rules \
  "[{\"destination\":\"0.0.0.0/0\",\"destinationType\":\"CIDR_BLOCK\",\"networkEntityId\":\"$IGW\"}]" >/dev/null
echo "$VCN"

paso "3/7 Firewall: 80 y 443 abiertos, 22 solo desde $IP_OFICINA"
tcp() { echo "{\"source\":\"$1\",\"protocol\":\"6\",\"isStateless\":false,\"tcpOptions\":{\"destinationPortRange\":{\"min\":$2,\"max\":$2}}}"; }
ENTRADA="[$(tcp "$IP_OFICINA/32" 22),$(tcp 0.0.0.0/0 80),$(tcp 0.0.0.0/0 443),\
{\"source\":\"0.0.0.0/0\",\"protocol\":\"1\",\"isStateless\":false,\"icmpOptions\":{\"type\":3,\"code\":4}}]"
SALIDA='[{"destination":"0.0.0.0/0","protocol":"all","isStateless":false}]'
SL=$(oci network security-list list -c "$C" --vcn-id "$VCN" --display-name idcmd-reglas --all | primero)
if [[ -z "$SL" ]]; then
  SL=$(oci network security-list create -c "$C" --vcn-id "$VCN" --display-name idcmd-reglas \
         --ingress-security-rules "$ENTRADA" --egress-security-rules "$SALIDA" \
         --wait-for-state AVAILABLE | jq -r .data.id)
else
  # Si se repite (p. ej. cambió la IP de la oficina), las reglas quedan como las define el script.
  oci network security-list update --security-list-id "$SL" --force \
    --ingress-security-rules "$ENTRADA" --egress-security-rules "$SALIDA" >/dev/null
fi
SUBRED=$(oci network subnet list -c "$C" --vcn-id "$VCN" --display-name idcmd-publica --all | primero)
if [[ -z "$SUBRED" ]]; then
  SUBRED=$(oci network subnet create -c "$C" --vcn-id "$VCN" --display-name idcmd-publica \
             --dns-label publica --cidr-block 10.0.0.0/24 --route-table-id "$RT" \
             --security-list-ids "[\"$SL\"]" --wait-for-state AVAILABLE | jq -r .data.id)
fi
echo "$SUBRED"

paso "4/7 Servidor 'idcmd-central' (Ubuntu 24.04 ARM, $OCPUS OCPU, $MEMORIA_GB GB, $DISCO_GB GB)"
INST=$(oci compute instance list -c "$C" --display-name idcmd-central --all \
         | jq -r '[.data[] | select(."lifecycle-state" != "TERMINATED")][0].id // empty')
if [[ -z "$INST" ]]; then
  AD=$(oci iam availability-domain list -c "$TENANCY" | jq -r '.data[0].name')
  IMAGEN=$(oci compute image list -c "$TENANCY" --operating-system "Canonical Ubuntu" \
             --operating-system-version "24.04" --shape "$FORMA" \
             --sort-by TIMECREATED --sort-order DESC --all | primero)
  [[ -n "$IMAGEN" ]] || { rojo "No encontré la imagen Ubuntu 24.04 para ARM."; exit 1; }
  INICIO=$(mktemp)
  # Primer arranque: abre 80/443 en el firewall del propio Ubuntu (las imágenes de Oracle traen
  # todo cerrado salvo el 22) e instala Docker desde los paquetes de Ubuntu.
  cat > "$INICIO" <<'EOF'
#cloud-config
timezone: America/Bogota
runcmd:
  - sed -i '/-A INPUT -j REJECT/i -A INPUT -p tcp -m state --state NEW -m tcp --dport 80 -j ACCEPT\n-A INPUT -p tcp -m state --state NEW -m tcp --dport 443 -j ACCEPT' /etc/iptables/rules.v4
  - iptables-restore < /etc/iptables/rules.v4
  - apt-get update
  - DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2 git jq
  - usermod -aG docker ubuntu
  - systemctl enable --now docker
EOF
  if ! INST=$(oci compute instance launch -c "$C" --availability-domain "$AD" \
         --display-name idcmd-central --hostname-label idcmd-central \
         --shape "$FORMA" --shape-config "{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEMORIA_GB}" \
         --image-id "$IMAGEN" --boot-volume-size-in-gbs "$DISCO_GB" \
         --subnet-id "$SUBRED" --assign-public-ip false \
         --ssh-authorized-keys-file "$LLAVE_ARCHIVO" --user-data-file "$INICIO" \
         --wait-for-state RUNNING | jq -r .data.id); then
    rojo "Oracle no creó el servidor. Si el error dice 'Out of host capacity', no hay ARM libre"
    rojo "en Bogotá en este momento: repite el script más tarde (lo ya creado se reutiliza)."
    exit 1
  fi
fi
echo "$INST"

paso "5/7 IP pública reservada 'idcmd-ip'"
VNIC=$(oci compute instance list-vnics --instance-id "$INST" | primero)
IP_PRIVADA=$(oci network private-ip list --vnic-id "$VNIC" | primero)
IP=$(oci network public-ip list -c "$C" --scope REGION --lifetime RESERVED --all \
       | jq -r '[.data[] | select(."display-name" == "idcmd-ip")][0]."ip-address" // empty')
if [[ -z "$IP" ]]; then
  IP=$(oci network public-ip create -c "$C" --lifetime RESERVED --display-name idcmd-ip \
         --private-ip-id "$IP_PRIVADA" --wait-for-state ASSIGNED | jq -r '.data."ip-address"')
fi
echo "$IP"

paso "6/7 Bucket privado 'idcmd-respaldos'"
if ! oci os bucket get --bucket-name idcmd-respaldos >/dev/null 2>&1; then
  oci os bucket create -c "$C" --name idcmd-respaldos --storage-tier Standard \
    --public-access-type NoPublicAccess >/dev/null
fi
echo "listo"

paso "7/7 Resumen"
rm -f "$LLAVE_ARCHIVO"
cat <<EOF
Todo creado en Bogotá (compartimento $COMPARTIMENTO).

  IP pública del servidor:  $IP

Falta solo lo tuyo:
  1. En el DNS de tu dominio: registro A  'command'  ->  $IP   (TTL 300)
  2. Espera unos 5 minutos (instala Docker) y prueba desde la oficina:
       ssh -i \$HOME\\.ssh\\idcmd_oci ubuntu@$IP
  3. Avísale a Claude "listo" con el nombre del dominio.
EOF
