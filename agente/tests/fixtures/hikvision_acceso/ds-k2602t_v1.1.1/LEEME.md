Panel Hikvision DS-K2602T, firmware V1.1.1 build 20230916 (oficina IDtech, 2026-10-27).

- `time.xml` y `AcsWorkStatus.json`: capturas reales, sin cambios.
- `deviceInfo.xml`: reconstruido desde la salida de `sondear`, con número de serie de ejemplo.

Lo que muestran (confirmado en sitio): hora local correcta pero zona UTC+0 y sin NTP; tapa
abierta; sin batería de respaldo (0 V); dos lectoras en línea en las direcciones RS-485 1 y 3
(`cardReaderOnlineStatus` es una lista de direcciones en este firmware).
