# ADR 0009 — Auditoría de accesos administrativos a los equipos

- Estado: aceptada por Pablo (2026-10-27), opción A (con cuenta e IP de origen).

## Contexto
Pablo pidió detectar cuándo alguien entra con un usuario administrador a un equipo para
buscar video o cambiar la configuración. Los NVR Dahua (`log.cgi`) y los paneles Hikvision
(`ContentMgmt/logSearch`) guardan un registro de operaciones que se puede leer sin
modificar nada.

## Decisión
- Adaptadores `dahua_registro` y `hikvision_registro`: cada 5 min, `deteccion: false`,
  lectura incremental con solape de 10 min e ids deterministas (reenviar no duplica).
- Se envía por operación: hora, tipo normalizado (inicio de sesión, fallido, cierre, cambio
  de configuración, búsqueda/reproducción/exportación de video, borrado de registros,
  reinicio, actualización de firmware), **cuenta** e **IP de origen** (opción A), y el
  código del fabricante.
- **Solo entradas de operación.** Los eventos (p. ej. marcaciones con tarjeta o empleado
  en un panel de acceso) se descartan en el agente y nunca salen de la red del cliente.
- **El agente usa su propia cuenta de solo lectura en cada equipo** (p. ej.
  `idcmd_monitor`). Sus inicios de sesión y consultas se omiten; sus operaciones CRÍTICAS
  (cambio de configuración, borrado, exportación, reinicio, firmware) se reportan: si
  alguien usa la credencial del agente para eso, se detecta.
- La hora se toma como hora de pared en `zona_horaria_equipo` (hay equipos con la zona mal
  configurada).
- En la central, `operaciones_equipo` es de solo inserción (nadie puede editarla ni
  borrarla) y respeta la RLS por sitio.

## Alertas (Etapa 3, sobre este historial)
Acceso fuera de horario · acceso desde IP no autorizada · cambio de configuración sin
ticket abierto · borrado de registros (siempre crítica) · intentos fallidos repetidos.

## Consecuencias y obligaciones
- Cuenta e IP pueden identificar a un operador: es dato personal (Ley 1581 de 2012). Su
  tratamiento, con finalidad de seguridad, debe constar en la política de tratamiento de
  datos de IDtech y en el contrato con cada cliente.
- Requiere crear la cuenta `idcmd_monitor` en cada equipo durante la instalación.
