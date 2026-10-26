"""Registro de un lote de un agente. Idempotente: reenviar el mismo lote no duplica nada.

Todo corre como `idcmd_ingesta` con `app.agente_id` fijado: aunque este código tuviera un
error, la RLS impide escribir datos de un sitio que no es el del agente.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from psycopg.types.json import Jsonb

from ..contrato import LoteIngesta
from ..db import AgenteAutenticado, BaseDatos

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResultadoIngesta:
    eventos_nuevos: int
    eventos_repetidos: int
    agregados_nuevos: int
    rechazados: int

    @property
    def aceptados(self) -> int:
        return self.eventos_nuevos + self.eventos_repetidos + self.agregados_nuevos


_INSERTAR_EVENTO = """
INSERT INTO idcmd.eventos_estado (id, dispositivo_id, cliente_id, sitio_id, agente_id, chequeo,
                                  estado_anterior, estado_nuevo, observado_at, codigo, metricas)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (id) DO NOTHING
RETURNING id
"""

# Un evento atrasado (llegó después de uno más reciente) no pisa el estado actual.
_ACTUALIZAR_ESTADO = """
INSERT INTO idcmd.estado_actual (dispositivo_id, chequeo, cliente_id, sitio_id, estado, desde,
                                 codigo, metricas, actualizado_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (dispositivo_id, chequeo) DO UPDATE
   SET estado = excluded.estado, desde = excluded.desde, codigo = excluded.codigo,
       metricas = excluded.metricas, actualizado_at = now()
 WHERE idcmd.estado_actual.desde <= excluded.desde
"""

_INSERTAR_AGREGADO = """
INSERT INTO idcmd.agregados_hora (id, dispositivo_id, cliente_id, sitio_id, agente_id, chequeo,
                                  hora, muestras_ok, muestras_degradado, muestras_caido,
                                  muestras_desconocido, latencia_prom_ms, latencia_max_ms)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (id) DO NOTHING
RETURNING id
"""

_LATIDO = """
UPDATE idcmd.agentes
   SET ultimo_latido_at = now(), inicio_sesion = %s, version = %s, pendientes = %s,
       desfase_reloj_s = extract(epoch FROM (%s::timestamptz - now()))
 WHERE id = %s
"""


async def ingerir(bd: BaseDatos, agente: AgenteAutenticado, lote: LoteIngesta) -> ResultadoIngesta:
    a = agente
    async with bd.como_agente(a.agente_id) as con:
        cur = await con.execute(
            "SELECT id FROM idcmd.dispositivos WHERE sitio_id = %s AND activo", (a.sitio_id,)
        )
        propios = {fila[0] for fila in await cur.fetchall()}

        eventos = [e for e in lote.eventos if e.dispositivo_id in propios]
        agregados = [g for g in lote.agregados if g.dispositivo_id in propios]
        rechazados = len(lote.eventos) + len(lote.agregados) - len(eventos) - len(agregados)
        if rechazados:
            # Inventario desalineado o agente mal configurado: se registra, no se reintenta.
            log.warning(
                "agente %s: %d ítems de dispositivos ajenos o inactivos", a.agente_id, rechazados
            )

        nuevos = 0
        for e in sorted(eventos, key=lambda e: e.observado_at):
            cur = await con.execute(
                _INSERTAR_EVENTO,
                (
                    e.id,
                    e.dispositivo_id,
                    a.cliente_id,
                    a.sitio_id,
                    a.agente_id,
                    e.chequeo,
                    e.estado_anterior.value,
                    e.estado_nuevo.value,
                    e.observado_at,
                    e.codigo,
                    Jsonb(e.metricas),
                ),
            )
            if await cur.fetchone() is None:
                continue
            nuevos += 1
            await con.execute(
                _ACTUALIZAR_ESTADO,
                (
                    e.dispositivo_id,
                    e.chequeo,
                    a.cliente_id,
                    a.sitio_id,
                    e.estado_nuevo.value,
                    e.observado_at,
                    e.codigo,
                    Jsonb(e.metricas),
                ),
            )

        agregados_nuevos = 0
        for g in agregados:
            cur = await con.execute(
                _INSERTAR_AGREGADO,
                (
                    g.id,
                    g.dispositivo_id,
                    a.cliente_id,
                    a.sitio_id,
                    a.agente_id,
                    g.chequeo,
                    g.hora,
                    g.muestras_ok,
                    g.muestras_degradado,
                    g.muestras_caido,
                    g.muestras_desconocido,
                    g.latencia_prom_ms,
                    g.latencia_max_ms,
                ),
            )
            agregados_nuevos += await cur.fetchone() is not None

        latido = lote.latido
        await con.execute(
            _LATIDO,
            (
                latido.inicio_sesion,
                latido.agente_version,
                latido.pendientes,
                latido.reloj_agente,
                a.agente_id,
            ),
        )

    return ResultadoIngesta(
        eventos_nuevos=nuevos,
        eventos_repetidos=len(eventos) - nuevos,
        agregados_nuevos=agregados_nuevos,
        rechazados=rechazados,
    )
