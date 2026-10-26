# ADR 0004 — Disponibilidad y reloj SLA

- Estado: aceptada (2026-10-26)

## Detección (agente)
- Antirrebote: 3 fallos seguidos para confirmar caída; 2 éxitos para confirmar recuperación.
- La hora del evento es la de la **primera** observación de la racha (inicio real).
- Peor caso de detección = intervalo × confirmaciones + 60 s de margen ≤ 300 s; la
  configuración que no lo cumple se rechaza al arrancar.
- Sin comunicación del agente ≠ caída: los equipos quedan DESCONOCIDO y el historial se
  completa al reconectar.

## Disponibilidad (central)
- `disponibilidad = tiempo_arriba / (tiempo_total − exclusiones)`.
- Exclusiones: ventanas de mantenimiento y caídas atribuibles al cliente. El tiempo sin datos
  se informa aparte y no cuenta como caída (supuesto P3).
- Sitio y regional: promedio ponderado por criticidad del equipo.
- Se calcula desde eventos: se puede recalcular hacia atrás si llegan datos atrasados.

## Ticket y reloj SLA
Estados: `abierto → en_atencion → resuelto → en_verificacion → cerrado`.
- Tiempo de respuesta: creación → primer paso a `en_atencion`. Tiempo de llegada: creación →
  primer check-in.
- Tiempo de solución: detección → `restablecido_at` de la última recuperación que superó la
  verificación (N min en línea confirmados por el agente; 30 por defecto, por contrato).
- Recaída durante la verificación: el ticket se reabre y el reloj nunca se detuvo.
- Recuperación espontánea antes de atención: pasa a verificación y se cierra como "sin
  intervención"; cuenta como caída, no como correctivo facturable.
- Reincidencia: misma falla del mismo equipo ≤ 24 h tras el cierre → ticket nuevo vinculado.
- Sin latidos del agente no hay verificación: el ticket espera.
