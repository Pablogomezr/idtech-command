# Respuestas de equipos para pruebas

Cada carpeta tiene respuestas de un tipo de equipo, por versión de firmware.

**Estado actual:** las respuestas fueron armadas a partir de la documentación pública de
cada fabricante, no capturadas de un equipo real. Hay que reemplazarlas por capturas de los
equipos de la oficina de IDtech:

    idcmd sondear --adaptador hikvision_acceso --direccion 192.168.1.80 --usuario admin \
        --capturar ./captura-hikvision

Antes de versionar una captura: revisar que no tenga números de serie reales, nombres de
personas ni datos del cliente (reemplazarlos por valores de ejemplo).
