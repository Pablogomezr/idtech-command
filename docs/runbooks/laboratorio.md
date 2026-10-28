# Laboratorio en un PC (Docker Desktop)

Base de datos + central (portal en http://localhost:8000) + agente vigilando equipos reales.
No es producción: sin HTTPS, un solo PC, la cookie de sesión no es "secure".

## Antes
1. Docker Desktop encendido ("Engine running").
2. Bóveda del agente con las claves de los equipos en `C:\idtech-lab\secretos`
   (`idcmd credenciales guardar <ref> --usuario <u> --secretos /secretos`).
3. Inventario del sitio en `C:\idtech-lab\inventario.yaml`
   (formato: `despliegue/laboratorio/inventario.ejemplo.yaml`). `credencial_ref` = nombre en la bóveda.

## Arranque
```powershell
powershell -ExecutionPolicy Bypass -File C:\idtech-command-vNN\despliegue\laboratorio\iniciar.ps1
```
La primera vez crea `C:\idtech-lab\lab.env` (secretos al azar del laboratorio), aplica las
migraciones, carga el inventario, crea el agente y su configuración (`C:\idtech-lab\agente`) y
pide el correo del usuario administrador del portal: muestra UNA vez la clave para Google o
Microsoft Authenticator.

## Qué verificar
- El portal muestra el sitio y "último contacto del agente: hace un momento".
- Cada equipo con sus chequeos; la página se actualiza sola cada 15 s.
- Auditoría: accesos a los equipos (cuenta, IP) y cambios de configuración detectados.

## Notas
- El agente comparte la red de la central (`network_mode: service:central`) para hablarle por
  `http://localhost:8000`: el agente solo acepta http sin TLS hacia localhost.
- Cambiar el inventario: editar `inventario.yaml` y volver a correr el script (es idempotente:
  los equipos se identifican por sitio + clave).
- Detener: `docker compose --env-file C:\idtech-lab\lab.env -f despliegue\laboratorio\docker-compose.yml down`
  (agregar `-v` borra la base y el buzón del agente).
