# ADR 0010 — Módulo de servicio: tickets automáticos, asignación por zona, SLA y seguimiento

- Estado: aceptada (2026-10-28, decisiones de Pablo)

## Contexto
Pablo pidió que, cuando un equipo caiga y el sistema lo detecte, se genere un servicio al
técnico de la zona con trazabilidad para los interesados (referencia: Auvo Service). Auvo
cubre bien el trabajo de campo, pero el ticket nace de una persona (portal, WhatsApp, correo).
La ventaja de IDtech Command es que la falla **abre el ticket sola** y el agente **verifica**
la reparación.

## Decisión (Parte 1)
- **Apertura automática** por el worker (`idcmd-central worker`, cada 30 s) de las fallas
  confirmadas por el agente: caídas, cámaras desconectadas o sin grabar, discos, tamper y
  batería (correctivo); reloj/zona horaria (preventivo, prioridad baja). Tabla en
  `servicio/reglas.py`. Un ticket abierto por equipo: las fallas nuevas se agregan a su línea
  de tiempo y pueden subir la prioridad. Sitio sin contacto del agente 5 min: ticket crítico.
- **Asignación**: técnico principal del sitio (`asignaciones_tecnico`); si no acepta en el
  tiempo de escalamiento de la prioridad, el suplente; luego coordinación (administradores).
- **SLA 24/7** por prioridad (respuesta / solución): crítica 2 h / 8 h, alta 4 h / 24 h,
  media 24 h / 72 h, baja 72 h / 7 d (`reglas_sla`, sobrescribible por cliente). Respuesta =
  el técnico acepta; solución = equipo verificado. El reloj corre desde el inicio de la falla
  según el agente, no desde que se abrió el ticket.
- **Cierre verificado**: "Resuelto" no cierra; el worker exige 10 min del equipo sano. Si a
  los 30 min sigue mal, el ticket se reabre. Si se recupera solo antes de que lo atiendan, se
  cierra como "recuperado sin intervención" (la caída igual cuenta para el SLA).
- **Avisos por correo y Telegram** (WhatsApp Business se evaluará después por costo y
  aprobación de Meta). Contenido mínimo (principio 5): número, sitio, equipo, qué pasó y
  enlace. Sin SMTP/Telegram configurados quedan en el portal ("Avisos") como "sin canal".
- **Seguimiento del cliente** por enlace de solo lectura, sin sesión: `/seguimiento/{id}/{firma}`
  con firma HMAC derivada de la llave del servidor (no se guarda en la base). Muestra estado,
  pasos, tiempos comprometidos e historial visible; nunca IPs ni notas internas. Caduca 30
  días después del cierre.
- **Permisos en la base**: las acciones del portal sobre un ticket pasan solo por
  `idcmd.accion_ticket` (valida rol, asignación y transición). El worker usa el rol
  `idcmd_worker` (sin BYPASSRLS, políticas explícitas).

## Parte 2 — servicio en campo (2026-10-28)
- **App instalable (PWA)** sobre el mismo portal: manifiesto, íconos y service worker con
  página "sin conexión". El service worker solo guarda lo estático, nunca páginas con datos.
- **Llegada al sitio con ubicación**, pedida SOLO al tocar "Llegué al sitio" (ADR 0003): se
  guarda la distancia al sitio (radio `radio_checkin_m`) y las coordenadas redondeadas a ~11 m.
  Si el técnico no la comparte, la llegada se registra igual y queda constancia.
- **Lista de pasos** copiada de la base de conocimiento al abrir el ticket (la fila más
  específica: marca + tipo > tipo > genérica). "Resuelto" exige todos los pasos en "hecho" o
  "no aplica" (disparador en la base). Las fallas que vienen en las métricas (un panel con
  tamper informa la batería solo en sus métricas) también se anotan con sus pasos.
- **Fotos** reducidas en el celular (1600 px, JPEG; al redibujarlas pierden el EXIF con la
  ubicación) y **firma** de quien recibe con el dedo. Se valida el contenido (JPEG/PNG), no la
  extensión; se guardan fuera de la base, direccionadas por su SHA-256 (una foto alterada en
  disco se detecta al leerla).
- **Acta del servicio** imprimible (o PDF desde el navegador) en el portal y en el enlace del
  cliente: datos, tiempos comprometidos, llegada, pasos, fotos, historial y firma.

## Pendiente (Parte 3)
Acta PDF generada en el servidor y enviada al cerrar; encuesta de satisfacción; cotización de
repuestos; WhatsApp; modo sin señal completo (cola de acciones en el celular).
