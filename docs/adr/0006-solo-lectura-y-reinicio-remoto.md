# ADR 0006 — Principio de solo lectura y la excepción del reinicio remoto

- Estado: aceptada (2026-10-26). El reinicio se implementa en Fase 2.

## Decisión
El sistema solo lee el estado de los equipos. Fuera de alcance de forma explícita: firmware,
cambio de contraseñas, configuración por lotes, video en vivo y reproducción.

Se impone en código: cada adaptador REST usa `ClienteSoloLectura` con lista blanca de
(método, ruta); en ONVIF la lista blanca es de acciones SOAP. Una prueba falla si se intenta
cualquier otra operación.

## Excepción: reinicio remoto (Fase 2)
1. Capacidad opcional `reiniciar` del adaptador, con una credencial separada.
2. Habilitado **localmente** en el agente, equipo por equipo (`permite_reinicio: true`).
3. Orden firmada Ed25519 por la central, caduca en 5 min, nonce de un solo uso; el agente la
   recoge en su sincronización (campo `comandos`, reservado y vacío en el contrato v1).
4. Doble confirmación (motivo + ticket + código MFA); aprobación de una segunda persona para
   equipos críticos. Nunca servidores Milestone desde el portal.
5. Límites en el agente: 1 reinicio por equipo cada 30 min y K por sitio por hora.
6. Registro en `comandos_remotos` y en la auditoría.
