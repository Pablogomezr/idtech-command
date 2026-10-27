# ADR 0008 — Totales de usuarios y configuración de seguridad de los equipos

- Estado: aceptada por Pablo (2026-10-27). Ajusta el principio de minimización (CLAUDE.md §5).

## Contexto
Pablo pidió saber cuántos usuarios hay registrados en cada panel de acceso y con qué método
se identifican. El principio original era "nada de datos de personas".

## Decisión
El principio queda así: **ni datos ni eventos de personas; sí totales y configuración de
seguridad del equipo.**

Permitido (con su operación exacta en la lista blanca):
- Totales: usuarios registrados (`UserInfo/Count`), tarjetas registradas (`cardNum` de
  `AcsWorkStatus`).
- Configuración de seguridad: modo de verificación de cada lectora (`cardReaderVerifyMode`).

Prohibido (bloqueado en la lista blanca, con prueba automática):
- Listar o buscar personas (`UserInfo/Search`), tarjetas (`CardInfo`), rostros
  (`FaceDataRecord`), huellas.
- Eventos de acceso / marcaciones (`AcsEvent`).

## Consecuencias
- Las métricas viajan como **instantáneas**: cuando cambia una métrica no volátil y al
  menos cada hora. La central guarda el valor actual (`metricas_actuales`) y el historial
  (`cambios_metricas`), con RLS por sitio.
- El historial permite alertar, en la Etapa 3: "cambió el número de usuarios sin un ticket
  de por medio" y "una lectora bajó su nivel de verificación".
