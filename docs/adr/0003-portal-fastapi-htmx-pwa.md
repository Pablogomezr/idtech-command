# ADR 0003 — Portal FastAPI + Jinja + HTMX renderizado en servidor, PWA para técnicos

- Estado: aceptada (2026-10-26)

## Decisión
Una sola aplicación Python sirve la API de ingesta, el portal y genera el PDF (WeasyPrint con
las mismas plantillas). Sin cadena de build de JavaScript. Vistas *mobile-first* para el
técnico, instalable como PWA (manifest + service worker).

## Razones
- Un solo lenguaje en agente, central e informes: mantenible por una persona.
- Renderizar en servidor permite auditar cada acceso (`auditoria_accesos`) y aplicar RLS con
  la identidad del usuario en cada transacción.

## Detalles
- PWA en línea con tolerancia: borradores locales y reintento de subida de fotos. Modo sin
  señal completo: Fase 2.
- Fotos con `<input capture="environment">`, comprimidas en el celular (~1.600 px), con hash.
- Check-in: geolocalización solo en el momento del check-in, nunca rastreo continuo.
- Informe al cliente: enlace al portal por defecto; adjunto PDF configurable por cliente.
- Notificaciones (correo, Telegram, web push): solo nombre del equipo, sitio, estado y enlace.
