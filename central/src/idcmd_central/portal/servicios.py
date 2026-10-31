"""Reporte de servicios (tickets): cuántos, de qué, en qué equipos, quién los atendió y si se
cumplió el SLA. Cada rol ve lo de sus sitios (RLS); el técnico, por defecto, solo los suyos."""

from __future__ import annotations

import csv
import io
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from psycopg.rows import dict_row

from ..informes import calculo
from ..informes import informe as inf
from . import consultas, presentacion
from .rutas import _a_ingreso, _ip, _pagina, _usuario_de_sesion

router = APIRouter()
AGRUPACIONES = {
    "equipo": "Equipo",
    "falla": "Tipo de falla",
    "tecnico": "Técnico",
    "prioridad": "Prioridad",
    "sitio": "Sitio",
}
MAX_LISTA = 300


def _mes(texto: str | None, defecto: date) -> date:
    if not texto:
        return defecto
    try:
        return inf.periodo_de(texto)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="mes no válido (AAAA-MM)") from e


async def _datos(request: Request, usuario_id: uuid.UUID, q: dict) -> dict:
    ahora = datetime.now(UTC)
    actual = inf.periodo_de(f"{ahora.astimezone(inf.ZONA):%Y-%m}")
    desde, hasta = _mes(q.get("desde"), actual), _mes(q.get("hasta"), actual)
    if hasta < desde:
        desde, hasta = hasta, desde
    por = q.get("por") if q.get("por") in AGRUPACIONES else "equipo"
    sitio = None
    if q.get("sitio"):
        try:
            sitio = uuid.UUID(q["sitio"])
        except ValueError as e:
            raise HTTPException(status_code=400) from e
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        usuario = await consultas.usuario(con)
        es_tecnico = usuario["rol"] == "tecnico"
        # El técnico ve por defecto sus servicios; puede ver todos los de sus sitios.
        mios = es_tecnico and q.get("todos") != "1"
        sitios = await consultas.sitios(con)
        cur = await con.cursor(row_factory=dict_row).execute(
            """
            SELECT t.numero, t.tipo, t.prioridad::text AS prioridad, t.estado::text AS estado,
                   t.cierre, t.codigo, t.titulo, t.abierto_at, t.aceptado_at, t.en_sitio_at,
                   t.resuelto_at, t.cerrado_at, t.vence_respuesta_at, t.vence_solucion_at,
                   s.nombre AS sitio, d.nombre AS equipo, d.tipo_equipo::text AS tipo_equipo,
                   u.nombre AS tecnico
              FROM idcmd.tickets t
              JOIN idcmd.sitios s ON s.id = t.sitio_id
              LEFT JOIN idcmd.dispositivos d ON d.id = t.dispositivo_id
              LEFT JOIN idcmd.usuarios u ON u.id = t.tecnico_id
             WHERE t.abierto_at >= %s AND t.abierto_at < %s
               AND (%s::uuid IS NULL OR t.sitio_id = %s::uuid)
               AND (NOT %s OR t.tecnico_id = %s)
             ORDER BY t.abierto_at DESC
            """,
            (inf.limites(desde)[0], inf.limites(hasta)[1], sitio, sitio, mios, usuario_id),
        )
        tickets = await cur.fetchall()
        await consultas.auditar(con, usuario_id, "ver", "/portal/servicios", _ip(request))

    def clave(t: dict) -> str:
        if por == "equipo":
            return t["equipo"] or f"{t['sitio']} (todo el sitio)"
        if por == "falla":
            return presentacion.nombre_codigo(t["codigo"])
        if por == "tecnico":
            return t["tecnico"] or "Sin asignar"
        if por == "prioridad":
            return presentacion.PRIORIDADES.get(t["prioridad"], t["prioridad"])
        return t["sitio"]

    grupos_t: dict[str, list[dict]] = defaultdict(list)
    for t in tickets:
        grupos_t[clave(t)].append(t)
    grupos = []
    for nombre, lista in grupos_t.items():
        ind = calculo.indicadores_tickets(lista, ahora)
        grupos.append({
            "nombre": nombre,
            "total": len(lista),
            "correctivos": sum(t["tipo"] == "correctivo" for t in lista),
            "verificados": sum(t["cierre"] == "verificado" for t in lista),
            "solos": sum(t["cierre"] == "recuperado_solo" for t in lista),
            "abiertos": sum(t["estado"] not in ("cerrado", "cancelado") for t in lista),
            "respuesta_pct": ind["respuesta"].porcentaje,
            "solucion_pct": ind["solucion"].porcentaje,
            "mttr_s": ind["mttr_s"],
        })  # fmt: skip
    grupos.sort(key=lambda g: (-g["total"], g["nombre"]))
    total = calculo.indicadores_tickets(tickets, ahora)
    return {
        "usuario": usuario,
        "sitios": sitios,
        "tickets": tickets,
        "grupos": grupos,
        "total": total,
        "filtro": {
            "desde": f"{desde:%Y-%m}",
            "hasta": f"{hasta:%Y-%m}",
            "por": por,
            "sitio": str(sitio) if sitio else "",
            "todos": "" if mios else "1",
        },  # fmt: skip
        "es_tecnico": es_tecnico,
        "mios": mios,
        "ahora": ahora,
    }


@router.get("/portal/servicios", response_class=HTMLResponse)
async def servicios(request: Request):
    usuario_id = await _usuario_de_sesion(request)
    if usuario_id is None:
        return _a_ingreso()
    d = await _datos(request, usuario_id, dict(request.query_params))
    return _pagina(
        request,
        "servicios.html",
        d
        | {
            "AGRUPACIONES": AGRUPACIONES,
            "MAX_LISTA": MAX_LISTA,
            "consulta": str(request.query_params),
        },  # fmt: skip
    )


def _hora(m: datetime | None) -> str:
    return presentacion.hora(m) if m else ""


def _a_tiempo(hecho, vence, ahora, cierre) -> str:
    if cierre in ("cancelado", "recuperado_solo"):
        return "no aplica"
    if hecho is not None:
        return "sí" if hecho <= vence else "no"
    return "no" if ahora > vence else "en curso"


@router.get("/portal/servicios.csv")
async def servicios_csv(request: Request):
    usuario_id = await _usuario_de_sesion(request)
    if usuario_id is None:
        return _a_ingreso()
    d = await _datos(request, usuario_id, dict(request.query_params))
    salida = io.StringIO()
    # Punto y coma y BOM: Excel en español (Colombia) abre el archivo con tildes y columnas.
    w = csv.writer(salida, delimiter=";")
    w.writerow(["Ticket", "Sitio", "Equipo", "Tipo de equipo", "Falla", "Tipo", "Prioridad",
                "Estado", "Técnico", "Abierto", "Aceptado", "En sitio", "Resuelto", "Cerrado",
                "Respuesta a tiempo", "Solución a tiempo", "Minutos hasta solución"])  # fmt: skip
    for t in d["tickets"]:
        minutos = (
            round((t["resuelto_at"] - t["abierto_at"]).total_seconds() / 60)
            if t["resuelto_at"] else ""
        )  # fmt: skip
        w.writerow([
            t["numero"], t["sitio"], t["equipo"] or "",
            presentacion.TIPOS_EQUIPO.get(t["tipo_equipo"], t["tipo_equipo"] or ""),
            presentacion.nombre_codigo(t["codigo"]), t["tipo"],
            presentacion.PRIORIDADES.get(t["prioridad"], t["prioridad"]),
            presentacion.CIERRES.get(t["cierre"]) if t["cierre"]
            else presentacion.ESTADOS_TICKET.get(t["estado"], t["estado"]),
            t["tecnico"] or "", _hora(t["abierto_at"]), _hora(t["aceptado_at"]),
            _hora(t["en_sitio_at"]), _hora(t["resuelto_at"]), _hora(t["cerrado_at"]),
            _a_tiempo(t["aceptado_at"], t["vence_respuesta_at"], d["ahora"], t["cierre"]),
            _a_tiempo(t["resuelto_at"], t["vence_solucion_at"], d["ahora"], t["cierre"]),
            minutos,
        ])  # fmt: skip
    f = d["filtro"]
    nombre = f"servicios-{f['desde']}" + (f"-a-{f['hasta']}" if f["hasta"] != f["desde"] else "")
    return Response(
        "﻿" + salida.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nombre}.csv"'},
    )
