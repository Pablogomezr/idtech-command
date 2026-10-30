# Parte 2: instalar la central en el servidor (HTTPS + respaldos)

Se hace **desde la oficina**, porque el puerto 22 solo admite la IP de la oficina. Toma unos 20
minutos. Sirve igual en Oracle Bogotá que en cualquier VPS Ubuntu 24.04 en Colombia.

Antes de empezar, debe estar lista la parte 1:

- servidor creado;
- registro A del dominio apuntando a su IP;
- `ssh` que entra con la llave `idcmd_oci`.

## 1. Instalar (≈15 min)

1. Descarga en **Descargas** estos dos archivos:
   - `idtech-command-vNN.tar.gz`
   - `subir-a-produccion.ps1`
2. En PowerShell (cambia el dominio y el número de versión):

   ```powershell
   powershell -ExecutionPolicy Bypass -File $env:USERPROFILE\Downloads\subir-a-produccion.ps1 -Servidor command.idtech.com.co -Version NN
   ```

3. Pide la frase de tu llave SSH (dos veces: al subir y al instalar). Luego, la primera vez,
   pregunta:
   - **Dominio:** `command.idtech.com.co`
   - **Correo para Let's Encrypt:** el de IDtech. Ahí avisan si un certificado no se pudo
     renovar.
   - **Correo, nombre y clave del administrador del portal:** la clave se pide dos veces y
     tiene mínimo 12 caracteres.
   - **Clave de configuración del segundo factor:** agrégala a tu app de autenticación en ese
     momento. Se muestra una sola vez.
4. Al final dice **`Listo: https://command.idtech.com.co/portal`**. Ábrelo. El candado del
   certificado puede tardar 1 o 2 minutos la primera vez.

Ya funciona desde cualquier PC o celular, con HTTPS. En el celular, la app del técnico se puede
instalar y usar la ubicación.

## 2. Guardar los secretos del servidor (≈5 min) — obligatorio

El instalador generó dos secretos que **solo existen en el servidor**:

- `/srv/idcmd/produccion.env`: claves de la base de datos y la llave que cifra los segundos
  factores.
- `/srv/idcmd/respaldo.clave`: la frase que cifra los respaldos.

**Sin ellos, un respaldo no sirve.** Si el servidor se pierde, no se podría restaurar nada.

Para verlos y guardarlos:

```powershell
ssh -i $HOME\.ssh\idcmd_oci ubuntu@command.idtech.com.co "sudo cat /srv/idcmd/produccion.env /srv/idcmd/respaldo.clave"
```

Copia el texto a una **nota segura de tu gestor de claves** (Bitwarden, 1Password, etc.).
Nunca lo pongas en el chat, en un correo ni en GitHub.

## 3. Respaldos fuera del servidor (≈10 min)

Desde que termina la instalación, el servidor ya respalda cada 6 horas:

- la base de datos, cada 6 horas;
- las fotos y firmas, una vez al día.

Esas copias quedan en el mismo servidor durante 7 días. Para que también queden copias cifradas
en el bucket de Bogotá:

1. En la consola de Oracle (región Bogotá), abre Cloud Shell y ejecuta:

   ```bash
   oci os bucket update --bucket-name idcmd-respaldos --versioning Enabled
   oci os preauth-request create --bucket-name idcmd-respaldos --name subir-respaldos \
     --access-type AnyObjectWrite --time-expires "$(date -u -d '+1 year' +%Y-%m-%dT00:00:00Z)" \
     | jq -r '.data["full-path"] // ("https://objectstorage.sa-bogota-1.oraclecloud.com" + .data["access-uri"])'
   ```

   - El versionado conserva las copias anteriores aunque alguien intente sobrescribirlas.
   - El segundo comando imprime un enlace que termina en `/o/`. Ese enlace **solo permite
     subir**: no permite leer ni borrar. Aun así es un secreto: cópialo sin compartirlo.
2. Entra al servidor y pégalo en la línea `IDCMD_RESPALDO_URL=`:

   ```powershell
   ssh -t -i $HOME\.ssh\idcmd_oci ubuntu@command.idtech.com.co "sudo nano /srv/idcmd/produccion.env"
   ```

   Guarda con `Ctrl+O`, `Enter` y sal con `Ctrl+X`.
3. Prueba un respaldo:

   ```powershell
   ssh -i $HOME\.ssh\idcmd_oci ubuntu@command.idtech.com.co "sudo idcmd-respaldar"
   ```

   Debe decir `subido: ...`. En la consola de Oracle, el bucket `idcmd-respaldos` muestra los
   archivos `.gpg`.
4. Agrega el enlace a tu gestor de claves, con la fecha de vencimiento. **Pon un recordatorio
   en el calendario para renovarlo antes de un año.**

## 4. Prueba de restauración (cada mes, ≈2 min)

```powershell
ssh -i $HOME\.ssh\idcmd_oci ubuntu@command.idtech.com.co "sudo bash idtech-command-vNN/despliegue/produccion/probar-restauracion.sh"
```

Restaura el respaldo más reciente en una base desechable, sin tocar la de producción. Muestra
cuántos sitios, equipos, usuarios y tickets tiene, y termina con **`Restauración correcta.`**.

Un respaldo que nunca se ha restaurado no es un respaldo.

## Actualizar a una versión nueva

Se usa el mismo comando del paso 1 con el número de versión nuevo. Conserva:

- datos;
- usuarios;
- certificados;
- secretos.

Las migraciones de la base se aplican solas. El portal deja de responder unos segundos y, durante
ese tiempo, los agentes guardan los datos en su buzón, así que no se pierde nada.

## Tareas frecuentes (en el servidor)

Entra con `ssh -i $HOME\.ssh\idcmd_oci ubuntu@command.idtech.com.co`:

| Para | Comando |
|---|---|
| Crear un usuario (técnico o cliente) | `sudo idcmd-central usuarios crear --correo ... --nombre "..." --rol tecnico` |
| Celular perdido / nuevo segundo factor | `sudo idcmd-central usuarios restablecer-mfa --correo ...` |
| Vincular Telegram | `sudo idcmd-central usuarios telegram --correo ... --chat-id ...` |
| Activar correo o Telegram | Editar `/srv/idcmd/produccion.env` (`IDCMD_SMTP_*`, `IDCMD_TELEGRAM_TOKEN`) y volver a correr la instalación |
| Ver el estado | `sudo docker ps` |
| Ver los registros | `sudo docker logs --tail 50 idcmd-central-1` (o `idcmd-worker-1`, `idcmd-caddy-1`) |
| Respaldo inmediato | `sudo idcmd-respaldar` |

## Si algo falla

- **No abre el portal o no tiene candado:**
  - revisa que `nslookup command.idtech.com.co` dé la IP del servidor;
  - revisa que los puertos 80 y 443 estén abiertos (parte 1, paso 4);
  - mira `sudo docker logs idcmd-caddy-1`.
- **`ssh` no conecta:** probablemente cambió la IP pública de la oficina. Actualiza la regla del
  puerto 22. También puedes volver a correr `crear-infraestructura.sh` en Cloud Shell: reutiliza
  todo y solo corrige las reglas.
- **Un paso del instalador se detiene en rojo:** envíale a Claude el texto rojo, que muestra la
  línea. No contiene claves.
