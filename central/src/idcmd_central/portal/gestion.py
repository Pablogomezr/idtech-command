"""Reportes de gestión: rendimiento de los técnicos y confiabilidad de los equipos.

El administrador ve a todos los técnicos; el técnico, solo su propia fila; el cliente no ve este
reporte (los nombres de los técnicos no son suyos). El de equipos lo ve cada rol en sus sitios.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from psycopg.rows import dict_row

from ..informes import calculo, gestion
from ..informes import informe as inf
from . import consultas, presentacion
from .rutas import _a_ingreso, _ip, _pagina, _usuario_de_sesion
from .servicios import _mes

router = APIRouter()


def _filtros(q: dict, ahora: datetime) -> dict:
    actual = inf.periodo_de(f"{ahora.astimezone(inf.ZONA):%Y-%m}")
    desde, hasta = _mes(q.get("desde"), actual), _mes(q.get("hasta"), actual)
    if hasta < desde:
        desde, hasta = hasta, desde
    sitio = None
    if q.get("sitio"):
        try:
            sitio = uuid.UUID(q["sitio"])
        except ValueError as e:
            raise HTTPException(status_code=400) from e
    inicio = inf.limites(desde)[0]
    fin = min(inf.limites(hasta)[1], calculo.truncar_hora(ahora - inf.MARGEN_LLEGADA))
    return {"desde": desde, "hasta": hasta, "sitio": sitio, "inicio": inicio, "fin": fin,
            "texto": {"desde": f"{desde:%Y-%m}", "hasta": f"{hasta:%Y-%m}",
                      "sitio": str(sitio) if sitio else ""}}  # fmt: skip


async def _tickets(con, f: dict, solo_tecnico: uuid.UUID | None) -> tuple[list[dict], list[dict]]:
    """(los del periodo, también 30 días antes y después para retrabajos y reincidencias)."""
    cur = await con.cursor(row_factory=dict_row).execute(
        """
        SELECT t.id, t.dispositivo_id, t.codigo, t.prioridad::text AS prioridad,
               t.estado::text AS estado, t.cierre, t.abierto_at, t.aceptado_at, t.en_sitio_at,
               t.resuelto_at, t.cerrado_at, t.vence_respuesta_at, t.vence_solucion_at,
               t.tecnico_id, u.nombre AS tecnico
          FROM idcmd.tickets t LEFT JOIN idcmd.usuarios u ON u.id = t.tecnico_id
         WHERE t.abierto_at >= %s AND t.abierto_at < %s
           AND (%s::uuid IS NULL OR t.sitio_id = %s::uuid)
        """,
        (f["inicio"] - gestion.VENTANA_RETRABAJO, f["fin"] + gestion.VENTANA_RETRABAJO,
         f["sitio"], f["sitio"]),
    )  # fmt: skip
    todos = await cur.fetchall()
    periodo = [
        t for t in todos
        if f["inicio"] <= t["abierto_at"] < f["fin"]
        and (solo_tecnico is None or t["tecnico_id"] == solo_tecnico)
    ]  # fmt: skip
    return periodo, todos


async def _contexto(request: Request, usuario_id: uuid.UUID):
    ahora = datetime.now(UTC)
    f = _filtros(dict(request.query_params), ahora)
    return ahora, f


# --- Técnicos ----------------------------------------------------------------------------------


async def _datos_tecnicos(request: Request, usuario_id: uuid.UUID) -> dict:
    ahora, f = await _contexto(request, usuario_id)
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        usuario = await consultas.usuario(con)
        if usuario["rol"] == "cliente":
            raise HTTPException(status_code=404)
        solo = usuario_id if usuario["rol"] == "tecnico" else None
        periodo, todos = await _tickets(con, f, solo)
        ids = [t["id"] for t in periodo]
        cur = await con.execute(
            "SELECT DISTINCT ticket_id FROM idcmd.eventos_ticket WHERE tipo = 'reapertura'"
            " AND ticket_id = ANY(%s)",
            (ids,),
        )
        reabiertos = {r[0] for r in await cur.fetchall()}
        sitios = await consultas.sitios(con)
        await consultas.auditar(con, usuario_id, "ver", "/portal/servicios/tecnicos", _ip(request))
    filas = gestion.rendimiento_tecnicos(periodo, todos, reabiertos, ahora)
    return {"usuario": usuario, "sitios": sitios, "filas": filas, "filtro": f["texto"],
            "pestana": "tecnicos", "dias_retrabajo": gestion.VENTANA_RETRABAJO.days}  # fmt: skip


@router.get("/portal/servicios/tecnicos", response_class=HTMLResponse)
async def tecnicos(request: Request):
    usuario_id = await _usuario_de_sesion(request)
    if usuario_id is None:
        return _a_ingreso()
    d = await _datos_tecnicos(request, usuario_id)
    return _pagina(request, "servicios_tecnicos.html", d | {"consulta": str(request.query_params)})


# --- Equipos -----------------------------------------------------------------------------------


async def _datos_equipos(request: Request, usuario_id: uuid.UUID) -> dict:
    _, f = await _contexto(request, usuario_id)
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        usuario = await consultas.usuario(con)
        sitios = await consultas.sitios(con)
        equipos, mediciones = [], {}
        for s in sitios:
            if f["sitio"] and s["id"] != f["sitio"]:
                continue
            lista, medidas, _ = await inf._medir_equipos(con, s["id"], f["inicio"], f["fin"])
            equipos += [e | {"sitio": s["nombre"]} for e in lista]
            mediciones |= medidas
        periodo, todos = await _tickets(con, f, None)
        await consultas.auditar(con, usuario_id, "ver", "/portal/servicios/equipos", _ip(request))
    filas = gestion.confiabilidad_equipos(equipos, mediciones, periodo, todos)
    return {"usuario": usuario, "sitios": sitios, "filas": filas, "filtro": f["texto"],
            "pestana": "equipos", "dias_reincidencia": gestion.VENTANA_RETRABAJO.days,
            "multisitio": len({e["sitio"] for e in equipos}) > 1}  # fmt: skip


@router.get("/portal/servicios/equipos", response_class=HTMLResponse)
async def equipos(request: Request):
    usuario_id = await _usuario_de_sesion(request)
    if usuario_id is None:
        return _a_ingreso()
    d = await _datos_equipos(request, usuario_id)
    return _pagina(request, "servicios_equipos.html", d | {"consulta": str(request.query_params)})


# --- Excel (CSV) -------------------------------------------------------------------------------


def _horas(s: float | None) -> str:
    return "" if s is None else f"{s / 3600:.1f}".replace(".", ",")


def _pct(v: float | None) -> str:
    return "" if v is None else f"{v:.1f}".replace(".", ",")


def _csv(nombre: str, encabezado: list[str], filas: list[list]) -> Response:
    salida = io.StringIO()
    w = csv.writer(salida, delimiter=";")  # Excel en español (Colombia)
    w.writerow(encabezado)
    w.writerows(filas)
    return Response(
        "﻿" + salida.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nombre}.csv"'},
    )


@router.get("/portal/servicios/tecnicos.csv")
async def tecnicos_csv(request: Request):
    usuario_id = await _usuario_de_sesion(request)
    if usuario_id is None:
        return _a_ingreso()
    d = await _datos_tecnicos(request, usuario_id)
    return _csv(
        f"rendimiento-tecnicos-{d['filtro']['desde']}-a-{d['filtro']['hasta']}",
        ["Técnico", "Servicios", "Atendidos", "Reparados", "En curso",
         "Respuesta media (h)", "Llegada media (h)", "Solución media (h)",
         "Respuesta a tiempo (%)", "Solución a tiempo (%)", "Reaperturas", "Retrabajos",
         "Resueltos a la primera (%)"],
        [[r["tecnico"], r["servicios"], r["atendidos"], r["reparados"], r["en_curso"],
          _horas(r["respuesta_s"]), _horas(r["llegada_s"]), _horas(r["solucion_s"]),
          _pct(r["respuesta_pct"]), _pct(r["solucion_pct"]), r["reaperturas"], r["retrabajos"],
          _pct(r["primera_pct"])] for r in d["filas"]],
    )  # fmt: skip


@router.get("/portal/servicios/equipos.csv")
async def equipos_csv(request: Request):
    usuario_id = await _usuario_de_sesion(request)
    if usuario_id is None:
        return _a_ingreso()
    d = await _datos_equipos(request, usuario_id)

    def dias(s):
        return "" if s is None else f"{s / 86400:.1f}".replace(".", ",")

    return _csv(
        f"confiabilidad-equipos-{d['filtro']['desde']}-a-{d['filtro']['hasta']}",
        ["Sitio", "Equipo", "Tipo", "Disponibilidad (%)", "Caídas", "Tiempo caído (h)",
         "Falla en promedio cada (días)", "Duración media de la caída (h)", "Última caída",
         "Servicios", "Visitas al sitio", "Reincidencias"],
        [[r["sitio"], r["equipo"], presentacion.TIPOS_EQUIPO.get(r["tipo"], r["tipo"]),
          _pct(r["disponibilidad"]), r["caidas"], _horas(r["caido_s"]), dias(r["mtbf_s"]),
          _horas(r["mttr_s"]), presentacion.hora(r["ultima_caida"]) if r["ultima_caida"] else "",
          r["servicios"], r["visitas"], r["reincidencias"]] for r in d["filas"]],
    )  # fmt: skip
