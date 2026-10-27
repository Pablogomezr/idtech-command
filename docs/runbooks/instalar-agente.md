# Runbook — Instalar el agente en un sitio

Requisito previo: **autorización escrita del cliente** archivada en el expediente del sitio.

## 1. Preparar el mini PC (en la oficina de IDtech)
1. Debian estable mínimo, disco completo cifrado con LUKS y desbloqueo por TPM2
   (`systemd-cryptenroll --tpm2-device=auto`). Secure Boot activo, contraseña de BIOS.
2. Instalar Docker Engine y `unattended-upgrades`.
3. Firewall: copiar `despliegue/minipc/nftables.conf`, ajustar subredes del cliente y la IP
   de la central, `systemctl enable --now nftables`.
4. Crear `/etc/idcmd/config` y `/etc/idcmd/secretos` (root, 0700).

## 2. Secretos
```bash
# Llave de la bóveda (una por sitio; nunca se copia a otro lado)
docker compose run --rm --user 0 -v /etc/idcmd/secretos:/s agente llave generar --salida /s/llave
# Token del agente: lo entrega la central al enrolar el agente (se muestra una sola vez)
printf '%s\n' '<token>' > /etc/idcmd/secretos/token
chown 10001:10001 /etc/idcmd/secretos/*
chmod 0400 /etc/idcmd/secretos/*
```

## 3. Configuración
Copiar `agente/config.ejemplo.yaml` a `/etc/idcmd/config/agente.yaml` y completar
`agente_id`, URL de la central y objetivos. **Nunca** poner claves en este archivo.
Si la red exige proxy, descomentar `HTTPS_PROXY` y `NO_PROXY` en `docker-compose.yml`; si el
proxy inspecciona TLS, montar la CA del cliente y declararla en `central.ca_adicional`.

## 4. Credenciales de equipos (en el sitio, por consola)
Crear en cada equipo una **cuenta propia del agente** (`idcmd_monitor`) con el mínimo
privilegio que permita leer estado y registro de operaciones. Nunca usar `admin`: la
auditoría de accesos (ADR 0009) necesita distinguir al agente de las personas.
Además, si la clave del agente queda mal o la cambian, el equipo bloquea la cuenta tras unos
pocos intentos fallidos (el NVR Dahua de la oficina se bloqueó ~25 min): con una cuenta
propia, el bloqueo nunca afecta a los operadores. Al cambiar la clave en el equipo, actualizar
la bóveda del agente el mismo día.

En los NVR, la cuenta del agente necesita permiso de **vista en vivo / monitoreo en TODOS los
canales** contratados (los NVR Dahua dan permisos por canal). Sin él, el estado o la
búsqueda de grabaciones de ese canal pueden fallar y parecer una cámara caída.

Verificación antes de dejar el sitio, con la cuenta del agente (no con `admin`), canal por
canal contratado: `dahua_nvr` debe dar cada canal `ok` y `dahua_retencion` debe dar `ok`
(o `retencion_proyectada_insuficiente` si el disco no alcanza), nunca `sin_grabacion_reciente`.
```bash
docker compose run --rm agente credenciales guardar milestone-principal --usuario svc_idcmd
docker compose run --rm agente credenciales listar
```

## 5. Arranque y verificación
```bash
docker compose up -d
docker compose exec agente idcmd --config /config/agente.yaml diagnostico   # debe salir 0
docker compose logs -f agente
```
Prueba de aceptación en sitio: desconectar un equipo de prueba y confirmar que el portal lo
muestra caído en ≤ 5 minutos. Reconectarlo y confirmar la recuperación.
