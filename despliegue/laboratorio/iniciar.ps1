# Laboratorio IDtech Command en este PC: base de datos + central (portal) + agente.
# Uso (PowerShell, con Docker Desktop encendido):
#   powershell -ExecutionPolicy Bypass -File C:\idtech-command-vNN\despliegue\laboratorio\iniciar.ps1
# Requiere: C:\idtech-lab\inventario.yaml (equipos) y C:\idtech-lab\secretos (bóveda del agente,
# creada con 'idcmd credenciales guardar'). Ninguna clave pasa por este script.
param(
  [string]$Lab = "C:\idtech-lab"
)
$Repo = (Resolve-Path "$PSScriptRoot\..\..").Path

docker info *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host "Docker Desktop no está encendido. Ábralo, espere 'Engine running' y repita." -ForegroundColor Red
  exit 1
}
if (-not (Test-Path "$Lab\inventario.yaml")) {
  Write-Host "Falta $Lab\inventario.yaml (el inventario de equipos del sitio)." -ForegroundColor Red
  exit 1
}
if (-not (Test-Path "$Lab\secretos\llave")) {
  Write-Host "Falta la bóveda del agente en $Lab\secretos (idcmd credenciales guardar)." -ForegroundColor Red
  exit 1
}

# Secretos del laboratorio: se generan una vez, al azar, y quedan solo en este PC.
$envFile = Join-Path $Lab "lab.env"
if (-not (Test-Path $envFile)) {
  $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
  function Azar([int]$n) { $b = New-Object byte[] $n; $rng.GetBytes($b); return ,$b }
  function Hex([int]$n) { -join ((Azar $n) | ForEach-Object { $_.ToString("x2") }) }
  @(
    "IDCMD_LAB=$($Lab -replace '\\', '/')",
    "IDCMD_PG_CLAVE=$(Hex 16)",
    "IDCMD_CLAVE_SERVICIO=$(Hex 16)",
    "IDCMD_LLAVE_TOTP=$([Convert]::ToBase64String((Azar 32)))"
  ) | Set-Content -Encoding ascii $envFile
  Write-Host "Secretos del laboratorio creados en $envFile (no lo comparta)." -ForegroundColor Yellow
}

$C = @("compose", "--env-file", $envFile, "-f", "$Repo\despliegue\laboratorio\docker-compose.yml")

Write-Host "`n== 1. Construir imágenes (la primera vez tarda unos minutos) ==" -ForegroundColor Cyan
docker @C build
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host "`n== 2. Base de datos, inventario y agente ==" -ForegroundColor Cyan
docker @C up -d bd
docker @C run --rm preparar
if ($LASTEXITCODE -ne 0) { exit 1 }

if (-not (Test-Path "$Lab\.usuario-creado")) {
  Write-Host "`n== 3. Su usuario del portal (administrador) ==" -ForegroundColor Cyan
  $correo = Read-Host "Correo para ingresar al portal"
  $nombre = Read-Host "Su nombre"
  docker @C run --rm -it preparar idcmd-central usuarios crear --correo $correo --nombre $nombre --rol admin
  if ($LASTEXITCODE -ne 0) { exit 1 }
  New-Item -ItemType File "$Lab\.usuario-creado" | Out-Null
  Write-Host "Agregue la clave de configuración a Google o Microsoft Authenticator AHORA." -ForegroundColor Yellow
  Read-Host "Presione Enter cuando la haya agregado"
} else {
  Write-Host "`n== 3. Usuario del portal: ya existe ==" -ForegroundColor Cyan
}

Write-Host "`n== 4. Central y agente ==" -ForegroundColor Cyan
docker @C up -d central agente
Start-Sleep -Seconds 5
docker @C ps
Start-Process "http://localhost:8000"
Write-Host "`nPortal: http://localhost:8000   (detener: docker $($C -join ' ') down)" -ForegroundColor Green
Write-Host "Registro del agente: docker $($C -join ' ') logs -f agente" -ForegroundColor Green
