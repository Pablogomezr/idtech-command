"""Informe mensual de SLA en el portal (con ingreso) y por enlace firmado para el cliente."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, date, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from ..informes import informe as inf
from ..informes.grafico import tendencia_svg
from ..informes.pdf import a_pdf
from ..servicio.reglas import enlace_informe, firma_informe, firma_informe_valida
from . import consultas
from .rutas import _a_ingreso, _ip, _pagina, _usuario_de_sesion, plantillas

router = APIRouter()
MESES_LISTA = 12


def _periodo(texto: str, ahora: datetime) -> date:
    try:
        periodo = inf.periodo_de(texto)
    except ValueError as e:
        raise HTTPException(status_code=404) from e
    if periodo > inf.limites(inf.periodo_de(f"{ahora:%Y-%m}"))[0].date() or periodo.year < 2020:
        raise HTTPException(status_code=404)
    return periodo


async def resumen(con, sitio_id: uuid.UUID, periodo: date, ahora: datetime) -> dict | None:
    """El informe emitido si el mes ya cerró y se emitió; si no, el cálculo al momento."""
    if inf.mes_cerrado(periodo, ahora):
        cur = await con.execute(
            "SELECT resumen FROM idcmd.informes_mensuales WHERE sitio_id = %s AND periodo = %s",
            (sitio_id, periodo),
        )
        fila = await cur.fetchone()
        if fila:
            return fila[0]
    return await inf.construir(con, sitio_id, periodo, ahora)


def _html(r: dict, barra: list[tuple[str, str, str]] | None = None) -> str:
    plantilla = plantillas.env.get_template("informe.html")
    return plantilla.render(r=r, barra=barra, grafico=tendencia_svg(r["tendencia"], r["meta"]))


def _nombre_archivo(r: dict) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", r["sitio"].lower()).strip("-")[:40] or "sitio"
    return f"informe-{base}-{r['periodo']}.pdf"


async def _pdf(r: dict) -> Response:
    contenido = await asyncio.to_thread(a_pdf, _html(r))
    return Response(
        contenido,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{_nombre_archivo(r)}"'},
    )


# --- Portal (con ingreso) --------------------------------------------------------------------


@router.get("/portal/informes", response_class=HTMLResponse)
async def lista(request: Request):
    usuario_id = await _usuario_de_sesion(request)
    if usuario_id is None:
        return _a_ingreso()
    ahora = datetime.now(UTC)
    actual = inf.periodo_de(f"{ahora.astimezone(inf.ZONA):%Y-%m}")
    periodos = [inf.mes_anterior(actual, n) for n in range(MESES_LISTA)]
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        usuario = await consultas.usuario(con)
        sitios = await consultas.sitios(con)
        cur = await con.execute(
            "SELECT sitio_id, periodo, disponibilidad::float FROM idcmd.informes_mensuales"
            " WHERE periodo >= %s",
            (periodos[-1],),
        )
        emitidos = {(s, p): d for s, p, d in await cur.fetchall()}
        await consultas.auditar(con, usuario_id, "ver", "/portal/informes", _ip(request))
    return _pagina(
        request,
        "informes.html",
        {
            "usuario": usuario,
            "sitios": sitios,
            "periodos": [(inf.texto_periodo(p), inf.nombre_periodo(p), p) for p in periodos],
            "emitidos": emitidos,
            "actual": actual,
        },
    )


async def _del_portal(request: Request, sitio_id: uuid.UUID, periodo_texto: str):
    usuario_id = await _usuario_de_sesion(request)
    if usuario_id is None:
        return None, None
    ahora = datetime.now(UTC)
    periodo = _periodo(periodo_texto, ahora)
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        usuario = await consultas.usuario(con)
        r = await resumen(con, sitio_id, periodo, ahora)
        if r is not None:
            await consultas.auditar(
                con, usuario_id, "ver", f"/portal/informes/{sitio_id}/{periodo_texto}", _ip(request)
            )
    if r is None:
        raise HTTPException(status_code=404)
    return usuario, r


@router.get("/portal/informes/{sitio_id}/{periodo}", response_class=HTMLResponse)
async def ver(request: Request, sitio_id: uuid.UUID, periodo: str):
    usuario, r = await _del_portal(request, sitio_id, periodo)
    if r is None:
        return _a_ingreso()
    barra = [("Descargar PDF", f"/portal/informes/{sitio_id}/{periodo}/pdf", "")]
    if usuario["rol"] == "admin" and not r["parcial"]:
        barra.append(("Enlace para el cliente", enlace_informe(sitio_id, periodo), "sec"))
    barra.append(("Volver", "/portal/informes", "sec"))
    return HTMLResponse(_html(r, barra))


@router.get("/portal/informes/{sitio_id}/{periodo}/pdf")
async def ver_pdf(request: Request, sitio_id: uuid.UUID, periodo: str):
    _, r = await _del_portal(request, sitio_id, periodo)
    if r is None:
        return _a_ingreso()
    return await _pdf(r)


# --- Enlace del cliente (sin ingreso): solo el informe ya emitido ----------------------------


async def _publico(request: Request, sitio_id: uuid.UUID, periodo: str, firma: str) -> dict:
    if len(firma) != 32 or not firma_informe_valida(sitio_id, periodo, firma):
        raise HTTPException(status_code=404)
    try:
        p = inf.periodo_de(periodo)
    except ValueError as e:
        raise HTTPException(status_code=404) from e
    async with request.app.state.bd.como_anonimo() as con:
        cur = await con.execute("SELECT idcmd.informe_emitido(%s, %s)", (sitio_id, p))
        fila = await cur.fetchone()
        if fila and fila[0]:
            await consultas.auditar(
                con, None, "informe", f"/informe/{sitio_id}/{periodo}", _ip(request)
            )
    if not fila or not fila[0]:
        raise HTTPException(status_code=404)
    return fila[0]


@router.get("/informe/{sitio_id}/{periodo}/{firma}", response_class=HTMLResponse)
async def publico(request: Request, sitio_id: uuid.UUID, periodo: str, firma: str):
    r = await _publico(request, sitio_id, periodo, firma)
    pdf = f"/informe/{sitio_id}/{periodo}/{firma_informe(sitio_id, periodo)}/pdf"
    return HTMLResponse(_html(r, [("Descargar PDF", pdf, "")]))


@router.get("/informe/{sitio_id}/{periodo}/{firma}/pdf")
async def publico_pdf(request: Request, sitio_id: uuid.UUID, periodo: str, firma: str):
    return await _pdf(await _publico(request, sitio_id, periodo, firma))
