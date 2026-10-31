"""Worker de servicio: cada ciclo abre, asigna, escala, verifica y cierra tickets, revisa el
SLA y arma y envía los avisos.

Todo con hora explícita (`ahora`) para poder probarlo, y como rol `idcmd_worker`.
Idempotente: correr dos ciclos seguidos no duplica tickets, eventos ni avisos.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from ..db import BaseDatos
from ..portal.presentacion import nombre_codigo
from . import avisos, conocimiento
from .reglas import REGLAS, mas_grave, prioridad

log = logging.getLogger(__name__)

SIN_CONEXION = timedelta(minutes=5)  # mismo umbral que el tablero
VERIFICACION = timedelta(minutes=10)  # el equipo debe llevar 10 min sano para cerrar
REINTENTO_VERIFICACION = timedelta(minutes=30)  # resuelto pero sigue mal -> se reabre
INTERVALO_S = 30


@dataclass
class Resumen:
    abiertos: int = 0
    asignados: int = 0
    escalados: int = 0
    cerrados: int = 0
    reabiertos: int = 0
    avisos: int = 0
    notas: list[str] = field(default_factory=list)


async def _evento(con, t: dict, ahora, tipo, texto, usuario_id=None, visible=True) -> None:
    await con.execute(
        "INSERT INTO idcmd.eventos_ticket (ticket_id, cliente_id, sitio_id, at, tipo, texto,"
        " usuario_id, visible_cliente) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (t["id"], t["cliente_id"], t["sitio_id"], ahora, tipo, texto, usuario_id, visible),
    )


# Respaldo si la tabla reglas_sla no tiene la fila (mismos valores que la migración 0006).
SLA_POR_DEFECTO = {
    "critica": {"respuesta_min": 120, "solucion_min": 480, "escalar_min": 15},
    "alta": {"respuesta_min": 240, "solucion_min": 1440, "escalar_min": 30},
    "media": {"respuesta_min": 1440, "solucion_min": 4320, "escalar_min": 120},
    "baja": {"respuesta_min": 4320, "solucion_min": 10080, "escalar_min": 480},
}


async def _regla_sla(con, cliente_id, prio) -> dict:
    cur = await con.cursor(row_factory=dict_row).execute(
        "SELECT respuesta_min, solucion_min, escalar_min FROM idcmd.reglas_sla"
        " WHERE prioridad = %s AND (cliente_id = %s OR cliente_id IS NULL)"
        " ORDER BY cliente_id NULLS LAST LIMIT 1",
        (prio, cliente_id),
    )
    return await cur.fetchone() or SLA_POR_DEFECTO[prio]


# --- 1. Abrir tickets -------------------------------------------------------------------------


async def _fallas_de_equipos(con) -> list[dict]:
    """Fallas que abren ticket: el código de cada chequeo y, además, las que vienen en las
    métricas del mismo chequeo (un panel informa un solo código — p. ej. tamper — aunque
    también le falte la batería; ambas deben quedar en el ticket)."""
    cur = await con.cursor(row_factory=dict_row).execute(
        """
        SELECT e.dispositivo_id, e.cliente_id, e.sitio_id, e.codigo, e.desde,
               d.nombre AS equipo, d.tipo_equipo::text AS tipo_equipo, d.criticidad, d.marca
          FROM idcmd.estado_actual e
          JOIN idcmd.dispositivos d ON d.id = e.dispositivo_id AND d.activo
         WHERE e.estado IN ('caido', 'degradado', 'desconocido') AND e.codigo = ANY(%(codigos)s)
        UNION
        SELECT m.dispositivo_id, m.cliente_id, m.sitio_id, x.codigo,
               coalesce(e.desde, m.observado_at), d.nombre, d.tipo_equipo::text, d.criticidad,
               d.marca
          FROM idcmd.metricas_actuales m
          JOIN idcmd.dispositivos d ON d.id = m.dispositivo_id AND d.activo
          LEFT JOIN idcmd.estado_actual e
                 ON e.dispositivo_id = m.dispositivo_id AND e.chequeo = m.chequeo
         CROSS JOIN LATERAL (VALUES
               ('tamper_activo', coalesce((m.metricas->>'tamper_activo')::boolean, false)),
               ('bateria_baja', coalesce((m.metricas->>'bateria_baja')::boolean, false)),
               ('lectora_desconectada',
                coalesce((m.metricas->>'lectoras_desconectadas')::int, 0) > 0),
               ('disco_con_error', coalesce((m.metricas->>'discos_con_error')::int, 0) > 0)
         ) AS x(codigo, activo)
         WHERE x.activo AND m.estado <> 'ok'
         ORDER BY 5
        """,
        {"codigos": list(REGLAS)},
    )
    return await cur.fetchall()


async def _agregar_pasos(con, t: dict, codigo: str, tipo_equipo, marca) -> None:
    for orden, texto in enumerate(conocimiento.pasos(codigo, tipo_equipo, marca), start=1):
        await con.execute(
            "INSERT INTO idcmd.pasos_ticket (ticket_id, cliente_id, sitio_id, codigo, orden,"
            " texto) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (t["id"], t["cliente_id"], t["sitio_id"], codigo, orden, texto),
        )


async def _sitios_sin_conexion(con, ahora) -> list[dict]:
    cur = await con.cursor(row_factory=dict_row).execute(
        """
        SELECT s.id AS sitio_id, s.cliente_id, max(a.ultimo_latido_at) AS ultimo
          FROM idcmd.sitios s JOIN idcmd.agentes a ON a.sitio_id = s.id AND a.revocado_at IS NULL
         GROUP BY s.id
        HAVING max(a.ultimo_latido_at) < %s
        """,
        (ahora - SIN_CONEXION,),
    )
    return await cur.fetchall()


async def _abrir(con, ahora, falla: dict, codigo: str, prio: str, tipo: str, titulo: str) -> None:
    regla = await _regla_sla(con, falla["cliente_id"], prio)
    inicio = falla["desde"]
    cur = await con.cursor(row_factory=dict_row).execute(
        """
        INSERT INTO idcmd.tickets (cliente_id, sitio_id, dispositivo_id, origen, tipo, prioridad,
            codigo, titulo, abierto_at, vence_respuesta_at, vence_solucion_at)
        VALUES (%s, %s, %s, 'automatico', %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        RETURNING id, numero, cliente_id, sitio_id
        """,
        (
            falla["cliente_id"],
            falla["sitio_id"],
            falla.get("dispositivo_id"),
            tipo,
            prio,
            codigo,
            titulo,
            inicio,
            inicio + timedelta(minutes=regla["respuesta_min"]),
            inicio + timedelta(minutes=regla["solucion_min"]),
        ),
    )
    t = await cur.fetchone()
    if t:
        donde = f"{falla['equipo']}: " if falla.get("equipo") else ""
        await _evento(con, t, ahora, "apertura", f"Falla detectada — {donde}{titulo}")
        await _agregar_pasos(con, t, codigo, falla.get("tipo_equipo"), falla.get("marca"))
    return t


async def abrir_tickets(con: AsyncConnection, ahora: datetime, r: Resumen) -> None:
    abiertos = {
        row[0]: row[1:]
        for row in await (
            await con.execute(
                "SELECT dispositivo_id, id, prioridad::text, codigo, cliente_id, sitio_id"
                " FROM idcmd.tickets WHERE estado NOT IN ('cerrado', 'cancelado')"
                " AND dispositivo_id IS NOT NULL"
            )
        ).fetchall()
    }
    # La falla más grave de cada equipo decide; las demás se suman a su línea de tiempo.
    por_equipo: dict[uuid.UUID, list[dict]] = {}
    for f in await _fallas_de_equipos(con):
        por_equipo.setdefault(f["dispositivo_id"], []).append(f)
    for dispositivo, fallas in por_equipo.items():
        evaluadas = [(prioridad(f["codigo"], f["tipo_equipo"], f["criticidad"]), f) for f in fallas]
        (prio, tipo), principal = max(
            evaluadas, key=lambda x: ["baja", "media", "alta", "critica"].index(x[0][0])
        )
        if dispositivo not in abiertos:
            nuevo = await _abrir(
                con,
                ahora,
                principal,
                principal["codigo"],
                prio,
                tipo,
                nombre_codigo(principal["codigo"]),
            )
            r.abiertos += 1
            if nuevo is None:
                continue
            tid, prio_actual, codigo_actual = nuevo["id"], prio, principal["codigo"]
            t = nuevo
        else:
            tid, prio_actual, codigo_actual, cliente_id, sitio_id = abiertos[dispositivo]
            t = {"id": tid, "cliente_id": cliente_id, "sitio_id": sitio_id}
        ya = {
            row[0]
            for row in await (
                await con.execute(
                    "SELECT texto FROM idcmd.eventos_ticket WHERE ticket_id = %s AND tipo ="
                    " 'novedad'",
                    (tid,),
                )
            ).fetchall()
        }
        for (_p, _), f in evaluadas:
            texto = f"Nueva novedad: {nombre_codigo(f['codigo'])}"
            if f["codigo"] != codigo_actual and texto not in ya:
                await _evento(con, t, ahora, "novedad", texto)
                await _agregar_pasos(con, t, f["codigo"], f["tipo_equipo"], f["marca"])
        nueva = mas_grave(prio_actual, prio)
        if nueva != prio_actual:
            await con.execute("UPDATE idcmd.tickets SET prioridad = %s WHERE id = %s", (nueva, tid))
            await _evento(con, t, ahora, "prioridad", f"Prioridad elevada a {nueva}")

    for s in await _sitios_sin_conexion(con, ahora):
        falla = {**s, "desde": s["ultimo"], "dispositivo_id": None}
        cur = await con.execute(
            "SELECT 1 FROM idcmd.tickets WHERE sitio_id = %s AND dispositivo_id IS NULL"
            " AND codigo = 'sitio_sin_conexion' AND estado NOT IN ('cerrado', 'cancelado')",
            (s["sitio_id"],),
        )
        if not await cur.fetchone():
            await _abrir(
                con,
                ahora,
                falla,
                "sitio_sin_conexion",
                "critica",
                "correctivo",
                "Sin conexión con el sitio (internet o energía)",
            )
            r.abiertos += 1


# --- 2. Asignar y escalar -----------------------------------------------------------------------


async def _tecnico(con, sitio_id, tipo: str, ahora) -> dict | None:
    cur = await con.cursor(row_factory=dict_row).execute(
        """
        SELECT u.id, u.nombre FROM idcmd.asignaciones_tecnico a
          JOIN idcmd.usuarios u ON u.id = a.tecnico_id AND u.activo
         WHERE a.sitio_id = %s AND a.tipo = %s AND a.vigente_desde <= %s
           AND (a.vigente_hasta IS NULL OR a.vigente_hasta > %s)
         ORDER BY a.vigente_desde DESC LIMIT 1
        """,
        (sitio_id, tipo, ahora, ahora),
    )
    return await cur.fetchone()


async def _asignar(con, t, tecnico, ahora, motivo: str) -> None:
    await con.execute(
        "UPDATE idcmd.tickets SET tecnico_id = %s, estado = 'asignado', asignado_at = %s"
        " WHERE id = %s",
        (tecnico["id"], ahora, t["id"]),
    )
    nombre = tecnico["nombre"].split(" ")[0]
    await _evento(con, t, ahora, "asignacion", f"{motivo}: {nombre}", tecnico["id"])


async def asignar_y_escalar(con: AsyncConnection, ahora: datetime, r: Resumen) -> None:
    cur = await con.cursor(row_factory=dict_row).execute(
        "SELECT t.*, t.prioridad::text AS prio FROM idcmd.tickets t"
        " WHERE t.estado IN ('abierto', 'asignado') ORDER BY t.abierto_at"
    )
    for t in await cur.fetchall():
        if t["estado"] == "abierto" and t["nivel_escalamiento"] == 0:
            principal = await _tecnico(con, t["sitio_id"], "principal", ahora)
            suplente = await _tecnico(con, t["sitio_id"], "suplente", ahora)
            if principal:
                await _asignar(con, t, principal, ahora, "Asignado al técnico de la zona")
            elif suplente:
                await con.execute(
                    "UPDATE idcmd.tickets SET nivel_escalamiento = 1 WHERE id = %s", (t["id"],)
                )
                await _asignar(con, t, suplente, ahora, "Asignado al técnico suplente")
            else:
                await con.execute(
                    "UPDATE idcmd.tickets SET nivel_escalamiento = 2 WHERE id = %s", (t["id"],)
                )
                await _evento(
                    con,
                    t,
                    ahora,
                    "sin_tecnico",
                    "El sitio no tiene técnico asignado: se avisó a coordinación",
                    visible=False,
                )
            r.asignados += 1
            continue
        if t["estado"] != "asignado" or t["nivel_escalamiento"] >= 2:
            continue
        regla = await _regla_sla(con, t["cliente_id"], t["prio"])
        if ahora - t["asignado_at"] < timedelta(minutes=regla["escalar_min"]):
            continue
        suplente = await _tecnico(con, t["sitio_id"], "suplente", ahora)
        if t["nivel_escalamiento"] == 0 and suplente and suplente["id"] != t["tecnico_id"]:
            await con.execute(
                "UPDATE idcmd.tickets SET nivel_escalamiento = 1 WHERE id = %s", (t["id"],)
            )
            await _asignar(
                con, t, suplente, ahora, "Sin respuesta del técnico; reasignado al suplente"
            )
        else:
            await con.execute(
                "UPDATE idcmd.tickets SET nivel_escalamiento = 2 WHERE id = %s", (t["id"],)
            )
            await _evento(
                con,
                t,
                ahora,
                "escalamiento",
                "Nadie aceptó el servicio a tiempo: escalado a coordinación",
                visible=False,
            )
        r.escalados += 1


# --- 3. Verificar y cerrar ------------------------------------------------------------------------


async def _sano_desde(con, t: dict, ahora) -> datetime | None:
    """Desde cuándo el equipo (o el sitio) está sin fallas que abran ticket; None si sigue mal."""
    if t["dispositivo_id"] is None:  # sin conexión: sano si el agente volvió a reportar
        fila = await (
            await con.execute(
                "SELECT max(ultimo_latido_at) FROM idcmd.agentes WHERE sitio_id = %s"
                " AND revocado_at IS NULL",
                (t["sitio_id"],),
            )
        ).fetchone()
        return fila[0] if fila[0] and ahora - fila[0] < timedelta(minutes=2) else None
    mal = await (
        await con.execute(
            "SELECT 1 FROM idcmd.estado_actual WHERE dispositivo_id = %s"
            " AND estado IN ('caido', 'degradado', 'desconocido') AND codigo = ANY(%s)",
            (t["dispositivo_id"], list(REGLAS)),
        )
    ).fetchone()
    if mal:
        return None
    fila = await (
        await con.execute(
            "SELECT max(desde) FROM idcmd.estado_actual WHERE dispositivo_id = %s",
            (t["dispositivo_id"],),
        )
    ).fetchone()
    return fila[0] or t["abierto_at"]


async def verificar_y_cerrar(con: AsyncConnection, ahora: datetime, r: Resumen) -> None:
    cur = await con.cursor(row_factory=dict_row).execute(
        "SELECT * FROM idcmd.tickets WHERE estado NOT IN ('cerrado', 'cancelado')"
    )
    for t in await cur.fetchall():
        sano = await _sano_desde(con, t, ahora)
        sitio = t["dispositivo_id"] is None
        estable = sano is not None and (sitio or ahora - sano >= VERIFICACION)
        if t["estado"] == "resuelto":
            if estable:
                await con.execute(
                    "UPDATE idcmd.tickets SET estado = 'cerrado', cerrado_at = %s,"
                    " cierre = 'verificado' WHERE id = %s",
                    (ahora, t["id"]),
                )
                await _evento(
                    con,
                    t,
                    ahora,
                    "cierre",
                    "El sistema verificó que el equipo funciona: servicio cerrado",
                )
                r.cerrados += 1
            elif sano is None and ahora - t["resuelto_at"] >= REINTENTO_VERIFICACION:
                await con.execute(
                    "UPDATE idcmd.tickets SET estado = 'aceptado', resuelto_at = NULL"
                    " WHERE id = %s",
                    (t["id"],),
                )
                await _evento(
                    con,
                    t,
                    ahora,
                    "reapertura",
                    "La verificación falló: el equipo sigue con falla; el servicio continúa",
                )
                r.reabiertos += 1
        elif t["estado"] in ("abierto", "asignado") and estable:
            # Nadie alcanzó a atenderlo: se cierra, pero la caída cuenta para el SLA.
            await con.execute(
                "UPDATE idcmd.tickets SET estado = 'cerrado', cerrado_at = %s,"
                " cierre = 'recuperado_solo' WHERE id = %s",
                (ahora, t["id"]),
            )
            await _evento(
                con,
                t,
                ahora,
                "recuperado_solo",
                "El equipo se recuperó sin intervención: servicio cerrado",
            )
            r.cerrados += 1


# --- 4. SLA ---------------------------------------------------------------------------------------


async def revisar_sla(con: AsyncConnection, ahora: datetime, r: Resumen) -> None:
    cur = await con.cursor(row_factory=dict_row).execute(
        """
        SELECT t.*,
               EXISTS (SELECT 1 FROM idcmd.eventos_ticket e WHERE e.ticket_id = t.id
                        AND e.tipo = 'sla_respuesta') AS avisado_respuesta,
               EXISTS (SELECT 1 FROM idcmd.eventos_ticket e WHERE e.ticket_id = t.id
                        AND e.tipo = 'sla_solucion') AS avisado_solucion
          FROM idcmd.tickets t WHERE t.estado NOT IN ('cerrado', 'cancelado')
        """
    )
    for t in await cur.fetchall():
        if (
            t["aceptado_at"] is None
            and ahora > t["vence_respuesta_at"]
            and not t["avisado_respuesta"]
        ):
            await _evento(
                con,
                t,
                ahora,
                "sla_respuesta",
                "Venció el tiempo de respuesta comprometido",
                visible=False,
            )
        if ahora > t["vence_solucion_at"] and not t["avisado_solucion"]:
            await _evento(
                con,
                t,
                ahora,
                "sla_solucion",
                "Venció el tiempo de solución comprometido",
                visible=False,
            )


# --- Ciclo ----------------------------------------------------------------------------------------


async def ciclo(bd: BaseDatos, ahora: datetime, enviar: bool = True) -> Resumen:
    r = Resumen()
    async with bd.como_worker() as con:
        await abrir_tickets(con, ahora, r)
        await asignar_y_escalar(con, ahora, r)
        await verificar_y_cerrar(con, ahora, r)
        await revisar_sla(con, ahora, r)
        r.avisos = await avisos.generar(con, ahora)
    if enviar:
        await avisos.enviar_pendientes(bd, ahora)
    return r


async def ejecutar(bd: BaseDatos) -> None:  # pragma: no cover - bucle de producción
    from datetime import UTC

    from ..informes.emitir import emitir_mes

    while True:
        try:
            r = await ciclo(bd, datetime.now(UTC))
            if r.abiertos or r.cerrados or r.escalados or r.reabiertos or r.avisos:
                log.info("servicio: %s", r)
        except Exception:
            log.exception("error en el ciclo del worker de servicio")
        try:
            if n := await emitir_mes(bd, datetime.now(UTC)):
                log.info("informes mensuales emitidos: %s", n)
        except Exception:
            log.exception("error al emitir los informes mensuales")
        await asyncio.sleep(INTERVALO_S)
