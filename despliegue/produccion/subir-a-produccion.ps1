# Sube una versión de IDtech Command al servidor y la instala (o actualiza). Desde su PC:
#   powershell -ExecutionPolicy Bypass -File subir-a-produccion.ps1 -Servidor command.idtech.com.co -Version 27
# Usa la llave SSH de la guía (C:\Users\<usted>\.ssh\idcmd_oci). Las preguntas (dominio, correo,
# clave del portal) se contestan en esta misma ventana; nada se guarda en su PC.
param(
  [Parameter(Mandatory)][string]$Servidor,
  [Parameter(Mandatory)][string]$Version,
  [string]$Llave = "$HOME\.ssh\idcmd_oci"
)
$paquete = "$env:USERPROFILE\Downloads\idtech-command-v$Version.tar.gz"
if (-not (Test-Path $paquete)) {
  Write-Host "No encuentro $paquete. Descárguelo primero a la carpeta Descargas." -ForegroundColor Red
  exit 1
}
if (-not (Test-Path $Llave)) {
  Write-Host "No encuentro la llave SSH $Llave (paso 5 de la guía de Oracle)." -ForegroundColor Red
  exit 1
}

if ($Servidor -notmatch '^\d+\.\d+\.\d+\.\d+$') {
  try { Resolve-DnsName $Servidor -Type A -ErrorAction Stop | Out-Null } catch {
    Write-Host "El nombre $Servidor todavía no existe en el DNS." -ForegroundColor Red
    Write-Host "Cree el registro A en su proveedor del dominio, o use la IP del servidor: -Servidor <IP>"
    exit 1
  }
}
if (-not (Test-NetConnection $Servidor -Port 22 -InformationLevel Quiet -WarningAction SilentlyContinue)) {
  Write-Host "El servidor $Servidor no responde en el puerto 22. Revise:" -ForegroundColor Red
  Write-Host "  - que sea la IP real del servidor (Oracle: Compute > Instances > idcmd-central);"
  Write-Host "  - que el servidor esté RUNNING;"
  Write-Host "  - que esté en la oficina: el puerto 22 solo admite la IP pública de la oficina."
  exit 1
}

Write-Host "== Subiendo la versión $Version a $Servidor ==" -ForegroundColor Cyan
scp -i $Llave $paquete "ubuntu@${Servidor}:"
if ($LASTEXITCODE -ne 0) {
  Write-Host "No se pudo subir (revise la frase de la llave SSH y el mensaje de arriba)." -ForegroundColor Red
  exit 1
}

Write-Host "== Instalando (la primera vez tarda unos minutos) ==" -ForegroundColor Cyan
$carpeta = "idtech-command-v$Version"
ssh -t -i $Llave "ubuntu@$Servidor" "rm -rf $carpeta && tar xzf $carpeta.tar.gz && sudo bash $carpeta/despliegue/produccion/instalar.sh"
if ($LASTEXITCODE -ne 0) { exit 1 }
