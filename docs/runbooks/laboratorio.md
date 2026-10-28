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

¿Perdió el celular o no guardó la clave de configuración? Repita con `-RestablecerMfa`: entrega
un segundo factor nuevo y cierra las sesiones abiertas (`idcmd-central usuarios restablecer-mfa`).

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

## Módulo de servicio (tickets)
El servicio `worker` abre un ticket cuando el agente confirma una falla, lo asigna al técnico
principal del sitio y avisa. Sin técnico asignado, el ticket queda "Abierto" y se avisa a los
administradores, que pueden "Tomar este servicio".

- Crear un técnico: `docker compose ... run --rm -it preparar idcmd-central usuarios crear
  --correo tecnico@... --nombre "..." --rol tecnico`, y en `inventario.yaml`:
  `tecnicos: [{correo: tecnico@..., tipo: principal}]`; volver a correr el script.
- Contactos del cliente (reciben el enlace de seguimiento): `contactos: [{nombre: ..., correo: ...}]`.
- Correo: agregar a `lab.env` `IDCMD_SMTP_SERVIDOR=smtp.gmail.com`, `IDCMD_SMTP_USUARIO=...`,
  `IDCMD_SMTP_CLAVE=<contraseña de aplicación de Google, no la del correo>`.
- Telegram: crear un bot con @BotFather, poner `IDCMD_TELEGRAM_TOKEN=...` en `lab.env`, y vincular
  cada usuario: `idcmd-central usuarios telegram --correo ... --chat-id <id>` (el id lo da @userinfobot).
- Sin correo ni Telegram configurados, los avisos se ven en el portal, menú "Avisos".

## App del técnico en el celular
- En el laboratorio el portal solo escucha en este PC (127.0.0.1). Para probar desde el celular
  en la misma red, la ubicación y la instalación como app exigen HTTPS: se habilita con el
  despliegue en la nube (OCI) o con un túnel HTTPS; mientras tanto se prueba en el navegador del PC.
- Para confirmar la llegada, el sitio necesita `lat` y `lon` en `inventario.yaml`.
