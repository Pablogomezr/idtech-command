# Conecta un sitio a la central de producción: carga su inventario en la central, trae la
# configuración del agente (sin claves) y arranca el agente en este PC apuntando a la central.
# Uso (PowerShell, Docker Desktop encendido, desde la oficina):
#   powershell -ExecutionPolicy Bypass -File C:\idtech-command-vNN\despliegue\sitio\conectar-sitio.ps1
# Se puede repetir (p. ej. tras cambiar el inventario): reutiliza el mismo agente y su token.
# Las claves de los equipos NO salen de este PC: siguen en la bóveda (carpeta -Secretos).
param(
  [string]$Servidor = "command.idtech.com.co",
  [string]$Inventario = "C:\idtech-lab\inventario.yaml",
  [string]$Secretos = "C:\idtech-lab\secretos",
  [string]$Sitio = "C:\idtech-sitio",
  [string]$Nombre = "oficina",
  [string]$Llave = "$HOME\.ssh\idcmd_oci"
)
$Repo = (Resolve-Path "$PSScriptRoot\..\..").Path
if ($Nombre -notmatch '^[a-z0-9_-]{1,40}$') {
  Write-Host "-Nombre solo admite minúsculas, números, - y _." -ForegroundColor Red; exit 1
}
docker info *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host "Docker Desktop no está encendido. Ábralo, espere 'Engine running' y repita." -ForegroundColor Red
  exit 1
}
foreach ($f in @($Inventario, "$Secretos\llave", $Llave)) {
  if (-not (Test-Path $f)) { Write-Host "Falta $f" -ForegroundColor Red; exit 1 }
}
$ssh = @("-i", $Llave)
$destino = "ubuntu@$Servidor"

Write-Host "`n== 1. Cargar el inventario en la central ($Servidor) ==" -ForegroundColor Cyan
scp @ssh $Inventario "${destino}:inventario-$Nombre.yaml"
if ($LASTEXITCODE -ne 0) { Write-Host "No se pudo subir el inventario (¿está en la oficina?)." -ForegroundColor Red; exit 1 }
$remoto = @(
  "set -e",
  "sudo install -d -m 755 /srv/idcmd/sitios",
  "sudo install -d -o 10001 -g 10001 -m 700 /srv/idcmd/sitios/$Nombre",
  "sudo install -m 644 inventario-$Nombre.yaml /srv/idcmd/sitios/$Nombre.yaml",
  "rm -f inventario-$Nombre.yaml",
  "sudo idcmd-central inventario cargar --inventario /sitios/$Nombre.yaml --salida /sitios/$Nombre --url-central https://$Servidor",
  "sudo install -m 600 -o ubuntu -t . /srv/idcmd/sitios/$Nombre/agente.yaml /srv/idcmd/sitios/$Nombre/token_agente"
) -join " && "
ssh -t @ssh $destino $remoto
if ($LASTEXITCODE -ne 0) { Write-Host "La central no pudo cargar el inventario (vea el mensaje de arriba)." -ForegroundColor Red; exit 1 }

Write-Host "`n== 2. Traer la configuración del agente (sin claves de equipos) ==" -ForegroundColor Cyan
New-Item -ItemType Directory -Force "$Sitio\agente" | Out-Null
scp @ssh "${destino}:agente.yaml" "${destino}:token_agente" "$Sitio\agente\"
$copiado = $LASTEXITCODE
ssh @ssh $destino "rm -f agente.yaml token_agente"   # el token no queda suelto en el servidor
if ($copiado -ne 0) { Write-Host "No se pudo traer la configuración." -ForegroundColor Red; exit 1 }

Write-Host "`n== 3. Agente de este sitio apuntando a la central ==" -ForegroundColor Cyan
# El agente del laboratorio vigila los mismos equipos: se detiene para no duplicar consultas
# (sus datos quedan; iniciar.ps1 lo vuelve a levantar si hace falta una demostración local).
$lab = docker ps -q --filter "label=com.docker.compose.project=idcmd-lab"
if ($lab) { docker stop $lab | Out-Null; Write-Host "Laboratorio local detenido." }
$env:IDCMD_SITIO = $Sitio -replace '\\', '/'
$env:IDCMD_SECRETOS = $Secretos -replace '\\', '/'
$C = @("compose", "-f", "$Repo\despliegue\sitio\docker-compose.yml")
docker @C up -d --build
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host "`nEsperando el primer envío (60 s)..."
Start-Sleep -Seconds 60
docker @C logs --tail 15 agente
Write-Host "`nListo. Abra https://$Servidor/portal : el sitio aparece con sus equipos." -ForegroundColor Green
Write-Host "Registro del agente:  docker $($C -join ' ') logs -f agente" -ForegroundColor Green
