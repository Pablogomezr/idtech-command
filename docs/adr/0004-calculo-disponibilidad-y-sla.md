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

## Informe mensual (implementado, Entrega 8)
Código: `central/src/idcmd_central/informes/calculo.py`; las reglas las fijan
`tests/test_informes_calculo.py`.
- **Equipo caído** si cualquiera de sus chequeos está caído; **arriba** si ninguno lo está y alguno
  está ok o degradado. Degradado ("con novedad", p. ej. retención baja) cuenta como disponible y se
  informa aparte.
- **Sin datos**: cada chequeo cuenta solo en las horas en que envió agregados (`agregados_hora`);
  fuera de ellas es desconocido. Así un agente apagado no se cobra como caída ni como disponible, y
  un chequeo retirado no deja su último estado congelado para siempre. Equipo sin ningún chequeo con
  datos = tiempo sin datos.
- **Orden**: exclusión (`exclusiones_sla`: mantenimiento o causa del cliente) > sin datos > estado.
- **Sitio**: promedio de equipos ponderado por criticidad (1 → 3, 2 → 2, 3 → 1). Un equipo se mide
  desde la hora en que empezó a reportar.
- **Tickets**: respuesta = aceptado antes de `vence_respuesta_at`; solución = resuelto antes de
  `vence_solucion_at`. Cancelados y recuperados solos no se evalúan (sí cuentan en disponibilidad).
- **Emisión**: el día 1 a las 6:00 (Bogotá) el worker guarda el informe del mes anterior
  (`informes_mensuales`) y avisa a contactos y coordinación. Lo emitido no cambia solo: los datos
  atrasados o una exclusión nueva requieren `idcmd-central informe emitir --recalcular`. La
  tendencia de 6 meses lee los informes emitidos (los agregados por hora se conservan 90 días).
