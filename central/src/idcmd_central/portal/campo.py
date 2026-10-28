"""App del técnico en campo (PWA): llegada con ubicación, lista de pasos, fotos, firma de
quien recibe y acta del servicio. Las validaciones de permisos viven en la base
(idcmd.llegada_ticket, marcar_paso, registrar_archivo); aquí solo se transporta.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import psycopg
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from ..servicio import almacen
from ..servicio.reglas import firma_valida
from . import consultas
from .rutas import _a_ingreso, _ip, _mismo_origen, _pagina, _usuario_de_sesion

router = APIRouter()
ESTATICOS = Path(__file__).parent / "estaticos"


def _volver(ticket_id: uuid.UUID, error: str | None = None, ancla: str = "") -> RedirectResponse:
    destino = f"/portal/tickets/{ticket_id}"
    if error:
        destino += f"?error={quote(error[:200])}"
    return RedirectResponse(destino + ancla, status_code=303)


def _mensaje(e: psycopg.Error) -> str:
    return (e.diag.message_primary if e.diag else None) or "acción no permitida"


async def _leer_subida(subida: UploadFile) -> bytes:
    contenido = await subida.read(almacen.MAX_BYTES + 1)
    if len(contenido) > almacen.MAX_BYTES:
        raise almacen.ArchivoInvalido("la foto pesa más de 8 MB")
    return contenido


@router.post("/portal/tickets/{ticket_id}/llegada")
async def llegada(
    request: Request,
    ticket_id: uuid.UUID,
    lat: float | None = Form(None, ge=-90, le=90),
    lon: float | None = Form(None, ge=-180, le=180),
    precision: int | None = Form(None, ge=0, le=100000),
):
    _mismo_origen(request)
    usuario_id = await _usuario_de_sesion(request)
    if usuario_id is None:
        return _a_ingreso()
    try:
        async with request.app.state.bd.como_usuario(usuario_id) as con:
            await con.execute(
                "SELECT idcmd.llegada_ticket(%s, %s, %s, %s)", (ticket_id, lat, lon, precision)
            )
    except psycopg.Error as e:
        return _volver(ticket_id, _mensaje(e))
    return _volver(ticket_id, ancla="#pasos")


@router.post("/portal/tickets/{ticket_id}/pasos/{paso_id}")
async def marcar_paso(
    request: Request,
    ticket_id: uuid.UUID,
    paso_id: uuid.UUID,
    estado: str = Form(..., pattern="^(pendiente|hecho|no_aplica)$"),
):
    _mismo_origen(request)
    usuario_id = await _usuario_de_sesion(request)
    if usuario_id is None:
        return _a_ingreso()
    try:
        async with request.app.state.bd.como_usuario(usuario_id) as con:
            await con.execute("SELECT idcmd.marcar_paso(%s, %s)", (paso_id, estado))
    except psycopg.Error as e:
        return _volver(ticket_id, _mensaje(e))
    return _volver(ticket_id, ancla="#pasos")


async def _registrar(request, ticket_id, contenido, tipo, nota=None, recibido_por=None):
    usuario_id = await _usuario_de_sesion(request)
    if usuario_id is None:
        return _a_ingreso()
    try:
        async with request.app.state.bd.como_usuario(usuario_id) as con:
            t = await consultas.ticket(con, ticket_id)
            if t is None:
                raise HTTPException(status_code=404)
            mime, huella = almacen.guardar(t["sitio_id"], contenido)
            await con.execute(
                "SELECT idcmd.registrar_archivo(%s, %s, %s, %s, %s, %s, %s)",
                (ticket_id, tipo, mime, len(contenido), huella, nota or None, recibido_por),
            )
    except almacen.ArchivoInvalido as e:
        return _volver(ticket_id, str(e))
    except psycopg.Error as e:
        return _volver(ticket_id, _mensaje(e))
    return _volver(ticket_id, ancla="#fotos" if tipo == "foto" else "#firma")


@router.post("/portal/tickets/{ticket_id}/fotos")
async def subir_foto(
    request: Request,
    ticket_id: uuid.UUID,
    foto: Annotated[UploadFile, File()],
    nota: str = Form("", max_length=300),
):
    _mismo_origen(request)
    try:
        contenido = await _leer_subida(foto)
    except almacen.ArchivoInvalido as e:
        return _volver(ticket_id, str(e))
    return await _registrar(request, ticket_id, contenido, "foto", nota=nota)


@router.post("/portal/tickets/{ticket_id}/firma")
async def subir_firma(
    request: Request,
    ticket_id: uuid.UUID,
    firma: Annotated[UploadFile, File()],
    recibido_por: str = Form(..., min_length=3, max_length=120),
):
    _mismo_origen(request)
    try:
        contenido = await _leer_subida(firma)
    except almacen.ArchivoInvalido as e:
        return _volver(ticket_id, str(e))
    return await _registrar(request, ticket_id, contenido, "firma", recibido_por=recibido_por)


@router.get("/portal/archivos/{archivo_id}")
async def ver_archivo(request: Request, archivo_id: uuid.UUID):
    usuario_id = await _usuario_de_sesion(request)
    if usuario_id is None:
        raise HTTPException(status_code=404)
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        a = await consultas.archivo(con, archivo_id)  # la RLS decide si lo puede ver
    if a is None:
        raise HTTPException(status_code=404)
    return _respuesta_archivo(a["sitio_id"], a["sha256"], a["mime"])


def _respuesta_archivo(sitio_id, sha256: bytes, mime: str) -> Response:
    try:
        contenido = almacen.leer(sitio_id, sha256)
    except (FileNotFoundError, almacen.ArchivoInvalido) as e:
        raise HTTPException(status_code=404) from e
    return Response(contenido, media_type=mime, headers={"Cache-Control": "private, max-age=86400"})


@router.get("/portal/tickets/{ticket_id}/acta", response_class=HTMLResponse)
async def acta(request: Request, ticket_id: uuid.UUID):
    usuario_id = await _usuario_de_sesion(request)
    if usuario_id is None:
        return _a_ingreso()
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        usuario = await consultas.usuario(con)
        t = await consultas.ticket(con, ticket_id)
        if t is None:
            raise HTTPException(status_code=404)
        contexto = {
            "usuario": usuario,
            "t": t,
            "eventos": [e for e in await consultas.eventos_ticket(con, ticket_id)
                        if e["visible_cliente"]],
            "pasos": await consultas.pasos_ticket(con, ticket_id),
            "archivos": await consultas.archivos_ticket(con, ticket_id),
            "url_archivo": lambda a: f"/portal/archivos/{a['id']}",
        }  # fmt: skip
        await consultas.auditar(
            con, usuario_id, "ver", f"/portal/tickets/{ticket_id}/acta", _ip(request)
        )
    return _pagina(request, "acta.html", contexto)


# --- Acta para el cliente (enlace de seguimiento) ------------------------------------------------


def _fecha(texto: str | None):
    from datetime import datetime

    return datetime.fromisoformat(texto) if texto else None


async def _datos_seguimiento(request, ticket_id):
    async with request.app.state.bd.como_anonimo() as con:
        cur = await con.execute("SELECT * FROM idcmd.seguimiento_ticket(%s)", (ticket_id,))
        fila = await cur.fetchone()
        nombres = [c.name for c in cur.description] if cur.description else []
        acta_json = (
            await (await con.execute("SELECT idcmd.acta_ticket(%s)", (ticket_id,))).fetchone()
        )[0]
    if not fila or not acta_json:
        raise HTTPException(status_code=404)
    return dict(zip(nombres, fila, strict=True)), acta_json


@router.get("/seguimiento/{ticket_id}/{firma}/acta", response_class=HTMLResponse)
async def acta_cliente(request: Request, ticket_id: uuid.UUID, firma: str):
    if len(firma) != 32 or not firma_valida(ticket_id, firma):
        raise HTTPException(status_code=404)
    t, datos = await _datos_seguimiento(request, ticket_id)
    contexto = {
        "usuario": None,
        "t": {
            **t,
            "recibido_por": datos["recibido_por"],
            "firmado_at": _fecha(datos["firmado_at"]),
            "en_sitio_at": _fecha(datos["en_sitio_at"]),
            "llegada_distancia_m": datos["llegada_distancia_m"],
        },
        "eventos": [{"at": e["at"], "texto": e["texto"]} for e in t["eventos"]],
        "pasos": datos["pasos"],
        "archivos": datos["archivos"],
        "url_archivo": lambda a: f"/seguimiento/{ticket_id}/{firma}/archivos/{a['id']}",
        "publica": True,
    }  # fmt: skip
    return _pagina(request, "acta_publica.html", contexto)


@router.get("/seguimiento/{ticket_id}/{firma}/archivos/{archivo_id}")
async def archivo_cliente(
    request: Request, ticket_id: uuid.UUID, firma: str, archivo_id: uuid.UUID
):
    if len(firma) != 32 or not firma_valida(ticket_id, firma):
        raise HTTPException(status_code=404)
    _, datos = await _datos_seguimiento(request, ticket_id)
    a = next((x for x in datos["archivos"] if x["id"] == str(archivo_id)), None)
    if a is None:  # solo archivos de ESTE ticket
        raise HTTPException(status_code=404)
    return _respuesta_archivo(uuid.UUID(datos["sitio_id"]), bytes.fromhex(a["sha256"]), a["mime"])


# --- PWA ---------------------------------------------------------------------------------------


@router.get("/portal/sw.js", include_in_schema=False)
async def service_worker():
    # Servido desde /portal/ para que su alcance cubra todo el portal.
    return FileResponse(
        ESTATICOS / "sw.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/portal/manifest.webmanifest", include_in_schema=False)
async def manifiesto():
    return FileResponse(ESTATICOS / "manifest.webmanifest", media_type="application/manifest+json")


@router.get("/portal/sin-conexion", response_class=HTMLResponse, include_in_schema=False)
async def sin_conexion(request: Request):
    return _pagina(request, "sin_conexion.html", {"usuario": None})
