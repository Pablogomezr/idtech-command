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
- En el laboratorio el portal solo escucha en este PC (127.0.0.1). La ubicación y la instalación
  como app exigen HTTPS: use `-Compartir` (abajo) o el despliegue en la nube.
- Para confirmar la llegada, el sitio necesita `lat` y `lon` en `inventario.yaml`.

## Presentarlo a clientes (otro PC o celular)
Mientras la central de producción no esté publicada, el laboratorio se puede mostrar así:

- **Desde cualquier lugar, con HTTPS** (celular con datos, PC del cliente):
  `iniciar.ps1 -Compartir`. Imprime un enlace `https://<palabras>.trycloudflare.com/portal`
  (túnel temporal de Cloudflare, sin cuenta ni puertos abiertos). En el celular funcionan la app
  del técnico, la ubicación y las fotos. El enlace cambia en cada arranque.
- **En la misma Wi-Fi de la oficina**, sin internet de por medio: `iniciar.ps1 -Red` y abrir
  `http://<IP del PC>:8000/portal` (sin HTTPS: sin ubicación ni instalación como app).
- Al terminar: `iniciar.ps1 -DejarDeCompartir` (el portal vuelve a solo este PC).

Cuidados: el enlace muestra la pantalla de ingreso a cualquiera que lo tenga (clave + código de
la app, 5 fallos bloquean 15 min); el tráfico del túnel pasa por Cloudflare, fuera de Colombia,
así que es solo para demostraciones con los equipos de la oficina, nunca con datos de clientes.
Para que el cliente vea "su" vista sin su propio celular de autenticación, muéstrele el
**enlace de seguimiento** de un ticket (solo lectura, sin ingreso).
