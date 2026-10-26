# ADR 0005 — Seguridad del agente y manejo de credenciales

- Estado: aceptada (2026-10-26)

## Decisión
- **Bóveda** AES-256-GCM (`cryptography`), referencia como dato autenticado, archivo 0600,
  escritura atómica. Llave en archivo aparte (Docker secret) sobre disco cifrado LUKS+TPM.
  Protege contra robo del disco; no contra root en el equipo (alcance honesto).
- **Guarda de secretos**: el token y todas las claves de la bóveda se registran; nada que los
  contenga entra al buzón (lo único que viaja a la central) y los logs los redactan.
- **Contrato estricto**: `extra="forbid"` y vocabulario común de métricas; un campo no
  declarado no puede viajar.
- **Token por agente**, 256 bits, mostrado una vez, guardado como hash en la central,
  revocable. 401/403 → el agente deja de insistir pero sigue recolectando.
- **Contenedor**: usuario 10001, raíz de solo lectura, `cap_drop: ALL`,
  `no-new-privileges`, ping con `net.ipv4.ping_group_range` (sin NET_RAW).
- **Equipo**: Debian mínimo, LUKS+TPM2, Secure Boot, contraseña de BIOS, nftables sin entrada
  (salida solo a la central, subredes de equipos, NTP y DNS), actualizaciones automáticas.
- **Mínimo privilegio en los equipos**: usuario ONVIF nivel operador, rol de solo lectura en
  Milestone, operador BioStar sin acceso a usuarios ni eventos si la versión lo permite.
- Acceso remoto de soporte: no existe por defecto; Tailscale solo con autorización escrita.
