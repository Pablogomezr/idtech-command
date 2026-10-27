"""Registro de un lote de un agente. Idempotente: reenviar el mismo lote no duplica nada.

Todo corre como `idcmd_ingesta` con `app.agente_id` fijado: aunque este código tuviera un
error, la RLS impide escribir datos de un sitio que no es el del agente.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from psycopg.types.json import Jsonb

from ..contrato import METRICAS_VOLATILES, Instantanea, LoteIngesta
from ..db import AgenteAutenticado, BaseDatos

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResultadoIngesta:
    eventos_nuevos: int
    eventos_repetidos: int
    agregados_nuevos: int
    rechazados: int
    instantaneas: int = 0
    cambios: int = 0
    operaciones_nuevas: int = 0
    operaciones_repetidas: int = 0

    @property
    def aceptados(self) -> int:
        return (
            self.eventos_nuevos
            + self.eventos_repetidos
            + self.agregados_nuevos
            + self.instantaneas
            + self.operaciones_nuevas
            + self.operaciones_repetidas
        )


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


def _estables(metricas: dict) -> dict:
    return {k: v for k, v in metricas.items() if k not in METRICAS_VOLATILES}


async def _registrar_instantanea(con, a: AgenteAutenticado, inst: Instantanea) -> int:
    """Actualiza el valor actual y, si cambió una métrica estable, registra el cambio.

    Devuelve 1 si hubo cambio. Una instantánea más vieja que la vigente se ignora (llegó
    tarde tras un corte), y reenviar la misma no duplica el cambio (id idempotente).
    """
    cur = await con.execute(
        "SELECT metricas, observado_at FROM idcmd.metricas_actuales"
        " WHERE dispositivo_id = %s AND chequeo = %s FOR UPDATE",
        (inst.dispositivo_id, inst.chequeo),
    )
    vigente = await cur.fetchone()
    if vigente is not None and vigente[1] >= inst.observado_at:
        return 0
    await con.execute(
        "INSERT INTO idcmd.metricas_actuales (dispositivo_id, chequeo, cliente_id, sitio_id,"
        " estado, metricas, observado_at) VALUES (%s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (dispositivo_id, chequeo) DO UPDATE SET estado = excluded.estado,"
        " metricas = excluded.metricas, observado_at = excluded.observado_at,"
        " recibido_at = now()",
        (
            inst.dispositivo_id,
            inst.chequeo,
            a.cliente_id,
            a.sitio_id,
            inst.estado.value,
            Jsonb(inst.metricas),
            inst.observado_at,
        ),
    )
    antes = _estables(vigente[0]) if vigente is not None else None
    despues = _estables(inst.metricas)
    if antes is None or antes == despues:
        return 0  # primera lectura o sin cambios: no es un cambio de configuración
    claves = {k for k in antes.keys() | despues.keys() if antes.get(k) != despues.get(k)}
    cur = await con.execute(
        "INSERT INTO idcmd.cambios_metricas (id, dispositivo_id, chequeo, cliente_id, sitio_id,"
        " agente_id, observado_at, antes, despues) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (id) DO NOTHING RETURNING id",
        (
            inst.id,
            inst.dispositivo_id,
            inst.chequeo,
            a.cliente_id,
            a.sitio_id,
            a.agente_id,
            inst.observado_at,
            Jsonb({k: antes.get(k) for k in claves}),
            Jsonb({k: despues.get(k) for k in claves}),
        ),
    )
    return int(await cur.fetchone() is not None)


async def ingerir(bd: BaseDatos, agente: AgenteAutenticado, lote: LoteIngesta) -> ResultadoIngesta:
    a = agente
    async with bd.como_agente(a.agente_id) as con:
        cur = await con.execute(
            "SELECT id FROM idcmd.dispositivos WHERE sitio_id = %s AND activo", (a.sitio_id,)
        )
        propios = {fila[0] for fila in await cur.fetchall()}

        eventos = [e for e in lote.eventos if e.dispositivo_id in propios]
        agregados = [g for g in lote.agregados if g.dispositivo_id in propios]
        instantaneas = [i for i in lote.instantaneas if i.dispositivo_id in propios]
        operaciones = [o for o in lote.operaciones if o.dispositivo_id in propios]
        recibidos = (
            len(lote.eventos) + len(lote.agregados) + len(lote.instantaneas) + len(lote.operaciones)
        )
        rechazados = recibidos - (
            len(eventos) + len(agregados) + len(instantaneas) + len(operaciones)
        )
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

        cambios = 0
        for inst in sorted(instantaneas, key=lambda i: i.observado_at):
            cambios += await _registrar_instantanea(con, a, inst)

        operaciones_nuevas = 0
        for o in operaciones:
            cur = await con.execute(
                "INSERT INTO idcmd.operaciones_equipo (id, dispositivo_id, cliente_id, sitio_id,"
                " agente_id, ocurrido_at, tipo, cuenta, ip_origen, codigo_fabricante)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (id) DO NOTHING RETURNING id",
                (
                    o.id,
                    o.dispositivo_id,
                    a.cliente_id,
                    a.sitio_id,
                    a.agente_id,
                    o.ocurrido_at,
                    o.tipo,
                    o.cuenta,
                    o.ip_origen,
                    o.codigo_fabricante,
                ),
            )
            operaciones_nuevas += await cur.fetchone() is not None

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
        instantaneas=len(instantaneas),
        cambios=cambios,
        operaciones_nuevas=operaciones_nuevas,
        operaciones_repetidas=len(operaciones) - operaciones_nuevas,
        rechazados=rechazados,
    )
