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

## Pendiente (Partes 2 y 3)
App del técnico (PWA): check-in con GPS, checklist por falla, fotos y firma; acta PDF por
servicio; encuesta; cotización de repuestos; WhatsApp.
