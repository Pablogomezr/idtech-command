"""Emisión del informe mensual: al cerrar el mes, el worker calcula el informe de cada sitio, lo
guarda (es lo que recibe el cliente y lo que se compara mes a mes) y avisa a los contactos."""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..db import BaseDatos
from ..portal.presentacion import ZONA, pct
from ..servicio.reglas import enlace_informe
from . import informe as inf

log = logging.getLogger(__name__)
# El día 1 a esta hora (Bogotá) el mes anterior ya está completo y los agentes que estuvieron
# sin internet han tenido unas horas para vaciar su buzón.
HORA_EMISION = 6


def periodo_a_emitir(ahora: datetime) -> date | None:
    local = ahora.astimezone(ZONA)
    if local.day == 1 and local.hour < HORA_EMISION:
        return None
    return inf.mes_anterior(date(local.year, local.month, 1))


async def guardar(
    con: AsyncConnection, sitio_id: uuid.UUID, periodo: date, ahora: datetime, recalcular: bool
) -> dict | None:
    r = await inf.construir(con, sitio_id, periodo, ahora)
    if r is None:
        return None
    await con.execute(
        "INSERT INTO idcmd.informes_mensuales (cliente_id, sitio_id, periodo, disponibilidad,"
        " resumen, generado_at) SELECT cliente_id, id, %s, %s, %s, %s FROM idcmd.sitios"
        " WHERE id = %s ON CONFLICT (sitio_id, periodo) DO "
        + ("UPDATE SET disponibilidad = excluded.disponibilidad, resumen = excluded.resumen,"
           " generado_at = excluded.generado_at" if recalcular else "NOTHING"),
        (periodo, r["disponibilidad"], Jsonb(r), ahora, sitio_id),
    )  # fmt: skip
    return r


async def _avisar(con: AsyncConnection, sitio: dict, periodo: date, r: dict, ahora) -> int:
    texto = inf.texto_periodo(periodo)
    asunto = f"[IDtech] Informe de servicio · {r['periodo_texto']} · {sitio['nombre']}"
    disp = "sin datos suficientes" if r["disponibilidad"] is None else pct(r["disponibilidad"])
    cuerpo = (
        f"Informe de servicio de {r['periodo_texto']}\n"
        f"Sitio: {sitio['nombre']}\n"
        f"Disponibilidad: {disp} (meta {pct(r['meta'])})\n"
        f"Servicios atendidos: {r['tickets']['total']}\n\n"
        f"Ver el informe: {enlace_informe(sitio['id'], texto)}\n\n"
        "IDtech Command — Ingeniería y Desarrollo IDtech SAS"
    )
    destinos: list[tuple[str, str, bool]] = []  # (canal, destino, ¿cliente?)
    cur = await con.execute(
        "SELECT correo, telegram_chat_id FROM idcmd.contactos_sitio WHERE sitio_id = %s AND activo",
        (sitio["id"],),
    )
    for correo, chat in await cur.fetchall():
        destinos += [("correo", str(correo), True)] if correo else []
        destinos += [("telegram", str(chat), True)] if chat else []
    cur = await con.execute(
        "SELECT correo, telegram_chat_id FROM idcmd.usuarios WHERE rol = 'admin' AND activo"
    )
    for correo, chat in await cur.fetchall():
        destinos += [("correo", str(correo), False)] if correo else []
        destinos += [("telegram", str(chat), False)] if chat else []
    adjunto = f"informe:{sitio['id']}:{texto}"
    vistos, n = set(), 0
    for canal, destino, es_cliente in destinos:
        if (canal, destino) in vistos:
            continue
        vistos.add((canal, destino))
        con_pdf = canal == "correo" and es_cliente and sitio["informe_pdf_adjunto"]
        await con.execute(
            "INSERT INTO idcmd.notificaciones (cliente_id, sitio_id, canal, destino, asunto,"
            " cuerpo, creada_at, adjunto) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (sitio["cliente_id"], sitio["id"], canal, destino, asunto, cuerpo, ahora,
             adjunto if con_pdf else None),
        )  # fmt: skip
        n += 1
    return n


async def emitir_mes(bd: BaseDatos, ahora: datetime) -> int:
    """Emite los informes pendientes del mes anterior. Devuelve cuántos emitió."""
    periodo = periodo_a_emitir(ahora)
    if periodo is None:
        return 0
    async with bd.como_worker() as con:
        cur = await con.cursor(row_factory=dict_row).execute(
            "SELECT s.id, s.cliente_id, s.nombre, c.informe_pdf_adjunto FROM idcmd.sitios s"
            " JOIN idcmd.clientes c ON c.id = s.cliente_id"
            " WHERE NOT EXISTS (SELECT 1 FROM idcmd.informes_mensuales i"
            "                    WHERE i.sitio_id = s.id AND i.periodo = %s)"
            "   AND EXISTS (SELECT 1 FROM idcmd.dispositivos d"
            "               WHERE d.sitio_id = s.id AND d.activo)",
            (periodo,),
        )
        pendientes = await cur.fetchall()
    emitidos = 0
    for sitio in pendientes:
        # Un sitio por transacción: si uno falla, los demás se emiten igual.
        try:
            async with bd.como_worker() as con:
                r = await guardar(con, sitio["id"], periodo, ahora, recalcular=False)
                # Sin nada medido ni servicios no se molesta al cliente (sitio sin agente aún).
                if r and (r["totales"]["medidos"] or r["tickets"]["total"]):
                    await _avisar(con, sitio, periodo, r, ahora)
            emitidos += 1
        except Exception:
            log.exception("no se pudo emitir el informe de %s (%s)", sitio["nombre"], periodo)
    return emitidos
