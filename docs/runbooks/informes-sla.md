# Informe mensual de SLA

## Qué es
Por sitio y por mes muestra:
- **disponibilidad** frente a la meta del contrato y **tendencia de 6 meses**;
- **disponibilidad por equipo** y **caídas más largas**;
- **cumplimiento de SLA** de los servicios (respuesta y solución por prioridad);
- **fallas más frecuentes** y **retención de video** frente a la contratada;
- **tiempo excluido** del cálculo.

Las reglas del cálculo están en el ADR 0004.

## Dónde se ve
- **Portal → Informes**: todos los meses de cada sitio, en pantalla y en PDF ("Descargar PDF").
  El mes en curso aparece como *parcial*.
- **Enlace del cliente**: el día 1 a las 6:00 (hora de Colombia) se emite el informe del mes
  anterior y se envía un aviso a los contactos del sitio y a coordinación. El aviso lleva solo la
  disponibilidad y el enlace firmado `https://command.idtech.com.co/informe/...`; el enlace abre el
  informe sin ingresar al portal.
- **PDF adjunto**: por defecto el aviso lleva solo el enlace (supuesto P7). Para un cliente que lo
  pida: `idcmd-central clientes sla --cliente "Nombre" --pdf-adjunto si`.

## Tareas (en el servidor: `ssh ... ubuntu@...` y luego `sudo idcmd-central ...`)

| Para | Comando |
|---|---|
| Ver sitios e ids | `idcmd-central sitios` |
| Meta de disponibilidad del contrato | `idcmd-central clientes sla --cliente "Nombre" --meta 99.5` |
| Mantenimiento acordado (no cuenta como caída) | `idcmd-central exclusiones crear --sitio "Sede" [--equipo "NVR"] --motivo mantenimiento --desde "2026-11-09 08:00" --hasta "2026-11-09 11:00" --descripcion "Cambio de UPS"` |
| Caída por causa del cliente | igual, con `--motivo causa_cliente` |
| Ver exclusiones | `idcmd-central exclusiones listar --sitio "Sede"` |
| Recalcular un mes ya emitido | `idcmd-central informe emitir --mes 2026-10 --sitio "Sede" --recalcular` |
| Guardar un PDF | `idcmd-central informe pdf --sitio "Sede" --mes 2026-10 --salida /tmp/informe.pdf` |

Las fechas se escriben en hora de Colombia. Las exclusiones aparecen en el informe con su
descripción: el cliente ve qué se excluyó y por qué.

Para recalcular, ten en cuenta:
- Recalcular cambia lo que el cliente ve en el enlace.
- Hazlo solo con una razón clara: datos atrasados de un agente que estuvo sin internet, o una
  exclusión acordada después de emitir el informe.
