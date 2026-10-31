"""Avisos de los tickets por correo y Telegram.

Contenido mínimo (principio 5): número de ticket, sitio, equipo, qué pasó y un enlace. Nunca
IPs, cuentas ni detalles técnicos. El técnico recibe el enlace al portal (con ingreso); el
cliente, el enlace de seguimiento de solo lectura.

Sin SMTP ni Telegram configurados (laboratorio), los avisos quedan en la bandeja con
resultado `sin_canal` y se ven en el portal (Avisos), para revisar qué se habría enviado.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import smtplib
import ssl
import urllib.request
from datetime import datetime
from email.message import EmailMessage

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from ..db import BaseDatos
from .reglas import enlace_portal, enlace_seguimiento

log = logging.getLogger(__name__)

# Qué eventos avisan a quién.
AL_TECNICO = {"asignacion"}
A_COORDINACION = {"sin_tecnico", "escalamiento", "sla_respuesta", "sla_solucion", "reapertura"}
AL_CLIENTE = {
    "apertura", "tomar", "aceptar", "en_camino", "en_sitio", "resuelto", "cierre",
    "recuperado_solo", "cancelar", "reapertura",
}  # fmt: skip
MAX_INTENTOS = 5


async def _destinatarios(con, ev: dict) -> list[tuple[str, str, str]]:
    """[(canal, destino, a_quien)] con a_quien en {'tecnico', 'coordinacion', 'cliente'}."""
    salida: list[tuple[str, str, str]] = []

    async def de_usuarios(sql: str, params, a_quien: str):
        cur = await con.execute(sql, params)
        for correo, chat in await cur.fetchall():
            if correo:
                salida.append(("correo", str(correo), a_quien))
            if chat:
                salida.append(("telegram", str(chat), a_quien))

    if ev["tipo"] in AL_TECNICO and ev["usuario_id"]:
        await de_usuarios(
            "SELECT correo, telegram_chat_id FROM idcmd.usuarios WHERE id = %s AND activo",
            (ev["usuario_id"],),
            "tecnico",
        )
    if ev["tipo"] in A_COORDINACION:
        await de_usuarios(
            "SELECT correo, telegram_chat_id FROM idcmd.usuarios WHERE rol = 'admin' AND activo",
            (),
            "coordinacion",
        )
    if ev["tipo"] in AL_CLIENTE:
        cur = await con.execute(
            "SELECT correo, telegram_chat_id FROM idcmd.contactos_sitio"
            " WHERE sitio_id = %s AND activo",
            (ev["sitio_id"],),
        )
        for correo, chat in await cur.fetchall():
            if correo:
                salida.append(("correo", str(correo), "cliente"))
            if chat:
                salida.append(("telegram", str(chat), "cliente"))
    return salida


def _mensaje(ev: dict, a_quien: str) -> tuple[str, str]:
    donde = ev["sitio"] + (f" · {ev['equipo']}" if ev["equipo"] else "")
    asunto = f"[IDtech] Ticket #{ev['numero']} · {donde}"
    enlace = (
        enlace_portal(ev["ticket_id"])
        if a_quien != "cliente"
        else enlace_seguimiento(ev["ticket_id"])
    )
    cuerpo = (
        f"Ticket #{ev['numero']} — {ev['titulo']}\n"
        f"Sitio: {ev['sitio']}\n"
        + (f"Equipo: {ev['equipo']}\n" if ev["equipo"] else "")
        + f"\n{ev['texto']}\n\n"
        + ("Ver y atender: " if a_quien == "tecnico" else "Seguimiento: ")
        + enlace
        + "\n\nIDtech Command — Ingeniería y Desarrollo IDtech SAS"
    )
    return asunto, cuerpo


async def generar(con: AsyncConnection, ahora: datetime) -> int:
    """Convierte los eventos nuevos de la línea de tiempo en avisos (una sola vez cada uno)."""
    fila = await (
        await con.execute("SELECT valor FROM idcmd.cursor_worker WHERE nombre = 'avisos'")
    ).fetchone()
    desde = fila[0] if fila else 0
    cur = await con.cursor(row_factory=dict_row).execute(
        """
        SELECT e.id, e.ticket_id, e.cliente_id, e.sitio_id, e.tipo, e.texto, e.usuario_id,
               t.numero, t.titulo, s.nombre AS sitio, d.nombre AS equipo, c.es_demo
          FROM idcmd.eventos_ticket e
          JOIN idcmd.tickets t ON t.id = e.ticket_id
          JOIN idcmd.sitios s ON s.id = e.sitio_id
          JOIN idcmd.clientes c ON c.id = e.cliente_id
          LEFT JOIN idcmd.dispositivos d ON d.id = t.dispositivo_id
         WHERE e.id > %s ORDER BY e.id
        """,
        (desde,),
    )
    creados, ultimo = 0, desde
    for ev in await cur.fetchall():
        ultimo = ev["id"]
        if ev["es_demo"]:
            continue  # cliente de demostración: sus tickets nunca avisan a nadie
        vistos = set()
        for canal, destino, a_quien in await _destinatarios(con, ev):
            if (canal, destino) in vistos:
                continue
            vistos.add((canal, destino))
            asunto, cuerpo = _mensaje(ev, a_quien)
            await con.execute(
                "INSERT INTO idcmd.notificaciones (ticket_id, cliente_id, sitio_id, canal,"
                " destino, asunto, cuerpo, creada_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (ev["ticket_id"], ev["cliente_id"], ev["sitio_id"], canal, destino, asunto,
                 cuerpo, ahora),
            )  # fmt: skip
            creados += 1
    if ultimo != desde:
        await con.execute(
            "INSERT INTO idcmd.cursor_worker (nombre, valor) VALUES ('avisos', %s)"
            " ON CONFLICT (nombre) DO UPDATE SET valor = excluded.valor",
            (ultimo,),
        )
    return creados


# --- Envío -----------------------------------------------------------------------------------


def _enviar_correo(
    destino: str, asunto: str, cuerpo: str, adjunto: tuple[str, bytes] | None = None
) -> str:
    servidor = os.environ.get("IDCMD_SMTP_SERVIDOR")
    if not servidor:
        return "sin_canal"
    msg = EmailMessage()
    msg["From"] = os.environ.get("IDCMD_SMTP_REMITENTE", os.environ.get("IDCMD_SMTP_USUARIO", ""))
    msg["To"] = destino
    msg["Subject"] = asunto
    msg.set_content(cuerpo)
    if adjunto:
        msg.add_attachment(adjunto[1], maintype="application", subtype="pdf", filename=adjunto[0])
    with smtplib.SMTP(servidor, int(os.environ.get("IDCMD_SMTP_PUERTO", "587")), timeout=20) as s:
        s.starttls(context=ssl.create_default_context())
        if usuario := os.environ.get("IDCMD_SMTP_USUARIO"):
            s.login(usuario, os.environ.get("IDCMD_SMTP_CLAVE", ""))
        s.send_message(msg)
    return "enviada"


def _enviar_telegram(chat_id: str, asunto: str, cuerpo: str, adjunto=None) -> str:
    # Telegram lleva solo el enlace (contenido mínimo); el PDF va únicamente por correo.
    token = os.environ.get("IDCMD_TELEGRAM_TOKEN")
    if not token:
        return "sin_canal"
    datos = json.dumps({"chat_id": int(chat_id), "text": f"{asunto}\n\n{cuerpo}"}).encode()
    pedido = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=datos,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(pedido, timeout=20) as r:  # noqa: S310 (URL fija, https)
        if r.status != 200:
            raise RuntimeError(f"telegram respondió {r.status}")
    return "enviada"


ENVIADORES = {"correo": _enviar_correo, "telegram": _enviar_telegram}


async def _adjunto(bd: BaseDatos, referencia: str) -> tuple[str, bytes]:
    """'informe:<sitio>:<aaaa-mm>' -> (nombre, PDF) generado desde el informe emitido."""
    from datetime import date

    from ..informes.pdf import a_pdf
    from ..portal.informes import _html, _nombre_archivo

    _, sitio, periodo = referencia.split(":")
    async with bd.como_worker() as con:
        cur = await con.execute(
            "SELECT resumen FROM idcmd.informes_mensuales WHERE sitio_id = %s AND periodo = %s",
            (sitio, date.fromisoformat(periodo + "-01")),
        )
        fila = await cur.fetchone()
    if fila is None:
        raise LookupError(f"no existe el informe {referencia}")
    return _nombre_archivo(fila[0]), await asyncio.to_thread(a_pdf, _html(fila[0]))


async def enviar_pendientes(bd: BaseDatos, ahora: datetime, lote: int = 50) -> int:
    async with bd.como_worker() as con:
        cur = await con.cursor(row_factory=dict_row).execute(
            "SELECT id, canal, destino, asunto, cuerpo, adjunto FROM idcmd.notificaciones"
            " WHERE enviada_at IS NULL AND intentos < %s ORDER BY id LIMIT %s",
            (MAX_INTENTOS, lote),
        )
        pendientes = await cur.fetchall()
    enviadas = 0
    for n in pendientes:
        try:
            adjunto = await _adjunto(bd, n["adjunto"]) if n["adjunto"] else None
            resultado = await asyncio.to_thread(
                ENVIADORES[n["canal"]], n["destino"], n["asunto"], n["cuerpo"], adjunto
            )
            error = None
        except Exception as e:  # el aviso se reintenta; el ciclo no se detiene
            resultado, error = None, f"{type(e).__name__}: {e}"[:200]
            log.warning("aviso %s por %s falló: %s", n["id"], n["canal"], error)
        async with bd.como_worker() as con:
            if resultado:
                await con.execute(
                    "UPDATE idcmd.notificaciones SET enviada_at = %s, resultado = %s,"
                    " intentos = intentos + 1 WHERE id = %s",
                    (ahora, resultado, n["id"]),
                )
                enviadas += resultado == "enviada"
            else:
                await con.execute(
                    "UPDATE idcmd.notificaciones SET intentos = intentos + 1, resultado = %s"
                    " WHERE id = %s",
                    (error, n["id"]),
                )
    return enviadas
