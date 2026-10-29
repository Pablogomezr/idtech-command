# Laboratorio IDtech Command en este PC: base de datos + central (portal) + agente.
# Uso (PowerShell, con Docker Desktop encendido):
#   powershell -ExecutionPolicy Bypass -File C:\idtech-command-vNN\despliegue\laboratorio\iniciar.ps1
# Requiere: C:\idtech-lab\inventario.yaml (equipos) y C:\idtech-lab\secretos (bóveda del agente,
# creada con 'idcmd credenciales guardar'). Ninguna clave pasa por este script.
# Con -RestablecerMfa entrega un segundo factor nuevo (celular perdido o clave no guardada).
# Con -Compartir abre además un enlace https temporal para ver el portal desde otro PC o un
# celular (demostraciones). Se cierra con -DejarDeCompartir.
# Con -Red el portal se abre también a los equipos de la misma Wi-Fi/LAN (sin internet de por
# medio): http://<IP de este PC>:8000.
param(
  [string]$Lab = "C:\idtech-lab",
  [switch]$RestablecerMfa,
  [switch]$Compartir,
  [switch]$DejarDeCompartir,
  [switch]$Red
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

if ($DejarDeCompartir) {
  docker @C --profile compartir rm -sf tunel
  $env:IDCMD_URL_PUBLICA = "http://localhost:8000"
  $env:IDCMD_ESCUCHAR = "127.0.0.1"
  docker @C up -d central worker agente
  Write-Host "Enlace público cerrado. El portal queda solo en http://localhost:8000" -ForegroundColor Green
  exit 0
}

Write-Host "`n== 1. Construir imágenes (la primera vez tarda unos minutos) ==" -ForegroundColor Cyan
docker @C build
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host "`n== 2. Base de datos, inventario y agente ==" -ForegroundColor Cyan
docker @C up -d bd
docker @C run --rm preparar
if ($LASTEXITCODE -ne 0) { exit 1 }

if ($RestablecerMfa) {
  Write-Host "`n== 3. Nuevo segundo factor para su usuario ==" -ForegroundColor Cyan
  $correo = Read-Host "Correo con que ingresa al portal"
  docker @C run --rm -it preparar idcmd-central usuarios restablecer-mfa --correo $correo
  if ($LASTEXITCODE -ne 0) { exit 1 }
  New-Item -ItemType File -Force "$Lab\.usuario-creado" | Out-Null
  Write-Host "Agregue la clave de configuración a Google o Microsoft Authenticator AHORA." -ForegroundColor Yellow
  Read-Host "Presione Enter cuando la haya agregado"
} elseif (-not (Test-Path "$Lab\.usuario-creado")) {
  Write-Host "`n== 3. Su usuario del portal (administrador) ==" -ForegroundColor Cyan
  $correo = Read-Host "Correo para ingresar al portal"
  $nombre = Read-Host "Su nombre"
  docker @C run --rm -it preparar idcmd-central usuarios crear --correo $correo --nombre $nombre --rol admin
  if ($LASTEXITCODE -ne 0) {
    Write-Host "Si el usuario ya existía, repita con -RestablecerMfa." -ForegroundColor Yellow
    exit 1
  }
  New-Item -ItemType File "$Lab\.usuario-creado" | Out-Null
  Write-Host "Agregue la clave de configuración a Google o Microsoft Authenticator AHORA." -ForegroundColor Yellow
  Read-Host "Presione Enter cuando la haya agregado"
} else {
  Write-Host "`n== 3. Usuario del portal: ya existe ==" -ForegroundColor Cyan
}

Write-Host "`n== 4. Central, servicio (tickets) y agente ==" -ForegroundColor Cyan
$portal = "http://localhost:8000"
$ipLocal = $null
if ($Red) {
  $ipLocal = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.PrefixOrigin -in @("Dhcp", "Manual") -and $_.IPAddress -notlike "169.254*" -and $_.InterfaceAlias -notmatch "vEthernet|WSL|Docker" } |
    Select-Object -First 1).IPAddress
  if (-not $ipLocal) { Write-Host "No encontré la IP de este PC en la red local." -ForegroundColor Red; exit 1 }
  $env:IDCMD_ESCUCHAR = "0.0.0.0"
  $env:IDCMD_URL_PUBLICA = "http://${ipLocal}:8000"
}
if ($Compartir) {
  docker @C up -d central
  docker @C --profile compartir up -d tunel
  Write-Host "Abriendo el enlace público (hasta 60 s)..."
  $url = $null
  for ($i = 0; $i -lt 30 -and -not $url; $i++) {
    Start-Sleep -Seconds 2
    # El enlace es https://<palabras-al-azar>.trycloudflare.com (no el de la API).
    $url = ((docker @C --profile compartir logs tunel 2>&1 | Out-String) |
      Select-String -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" -AllMatches).Matches |
      ForEach-Object { $_.Value } | Where-Object { $_ -ne "https://api.trycloudflare.com" } |
      Select-Object -First 1
  }
  if (-not $url) {
    Write-Host "No se obtuvo el enlace. Revise: docker $($C -join ' ') --profile compartir logs tunel" -ForegroundColor Red
    exit 1
  }
  # Los enlaces de los avisos y del seguimiento del cliente apuntan al enlace público.
  $env:IDCMD_URL_PUBLICA = $url
  $portal = $url
}
docker @C up -d central worker agente
Start-Sleep -Seconds 5
docker @C ps
Start-Process $portal
if ($Compartir) {
  Write-Host "`n=== ENLACE PARA OTRO PC O CELULAR ===" -ForegroundColor Yellow
  Write-Host "  $url/portal" -ForegroundColor Yellow
  Write-Host "Cualquiera con el enlace ve la pantalla de ingreso (clave + código de la app)."
  Write-Host "El enlace cambia cada vez. Al terminar la presentación ciérrelo con:"
  Write-Host "  powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -DejarDeCompartir"
}
if ($Red) {
  Write-Host "`n=== DESDE OTRO PC O CELULAR EN LA MISMA WI-FI ===" -ForegroundColor Yellow
  Write-Host "  http://${ipLocal}:8000/portal" -ForegroundColor Yellow
  Write-Host "Si no abre, permita Docker Desktop en el Firewall de Windows (redes privadas)."
  Write-Host "Sin https el celular no instala la app del técnico ni comparte la ubicación (use -Compartir)."
  Write-Host "Para volver a solo este PC: -DejarDeCompartir"
}
Write-Host "`nPortal: http://localhost:8000   (detener: docker $($C -join ' ') down)" -ForegroundColor Green
Write-Host "Registro del agente: docker $($C -join ' ') logs -f agente" -ForegroundColor Green
