Panel Hikvision DS-K2602T, firmware V1.1.1 build 20230916 (oficina IDtech, 2026-10-27).

- `time.xml` y `AcsWorkStatus.json`: capturas reales, sin cambios.
- `deviceInfo.xml`: reconstruido desde la salida de `sondear`, con número de serie de ejemplo.

Lo que muestran: hora local correcta pero zona configurada UTC+0 y sin NTP; caja abierta;
batería baja (0 V: probablemente sin batería conectada); lectoras `[1, 3]` (valor 3 sin
documentación pública: pendiente de confirmar qué significa).
