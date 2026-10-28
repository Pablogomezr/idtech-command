"""Portal web: ingreso con clave + TOTP, estado en vivo por sitio y auditoría.

Cada página consulta con la identidad del usuario (`bd.como_usuario`), así que la RLS de
Postgres decide qué ve: un técnico solo sus sitios asignados, un cliente solo su alcance.
Cada ingreso, salida y página vista queda en `auditoria_accesos`.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..servicio.reglas import firma_valida
from . import consultas, presentacion
from .seguridad import (
    descifrar_secreto,
    generar_token_sesion,
    hash_sesion,
    llave_desde_entorno,
    verificar_totp,
)

COOKIE = "idcmd_sesion"
DURACION_SESION = timedelta(hours=12)
REFRESCO_S = 15

plantillas = Jinja2Templates(directory=Path(__file__).parent / "plantillas")
plantillas.env.globals.update(
    ESTADOS=presentacion.ESTADOS,
    CHEQUEOS=presentacion.CHEQUEOS,
    TIPOS_EQUIPO=presentacion.TIPOS_EQUIPO,
    ESTADOS_TICKET=presentacion.ESTADOS_TICKET,
    PRIORIDADES=presentacion.PRIORIDADES,
    CIERRES=presentacion.CIERRES,
    ACCIONES_TICKET=presentacion.ACCIONES_TICKET,
    plazo=presentacion.plazo,
    duracion=presentacion.duracion,
    METRICAS=presentacion.METRICAS,
    OPERACIONES=presentacion.OPERACIONES,
    CRITICAS=presentacion.CRITICAS,
    REFRESCO_S=REFRESCO_S,
)
plantillas.env.filters.update(
    hora_iso=lambda texto, formato="%d/%m %H:%M": presentacion.hora(
        datetime.fromisoformat(texto), formato
    ),
    hora=presentacion.hora,
    codigo=presentacion.nombre_codigo,
    legible=presentacion.legible,
    valor=presentacion.valor,
)

router = APIRouter()


def _cookie_segura() -> bool:
    # Solo el laboratorio (http://localhost) la desactiva; en producción va detrás de HTTPS.
    return os.environ.get("IDCMD_COOKIE_SEGURA", "true").lower() != "false"


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _mismo_origen(request: Request) -> None:
    """Los formularios solo se aceptan desde el propio portal (además de SameSite=Strict)."""
    origen = request.headers.get("origin") or request.headers.get("referer")
    if origen and urlsplit(origen).netloc != request.headers.get("host"):
        raise HTTPException(status_code=403, detail="origen no permitido")


async def _usuario_de_sesion(request: Request) -> uuid.UUID | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    async with request.app.state.bd.como_anonimo() as con:
        cur = await con.execute("SELECT idcmd.usuario_de_sesion(%s)", (hash_sesion(token),))
        fila = await cur.fetchone()
    return fila[0] if fila else None


def _a_ingreso() -> RedirectResponse:
    return RedirectResponse("/portal/entrar", status_code=303)


def _pagina(request: Request, nombre: str, contexto: dict, estado: int = 200) -> HTMLResponse:
    return plantillas.TemplateResponse(
        request, nombre, {"ahora": datetime.now(UTC), "hace": presentacion.hace, **contexto},
        status_code=estado,
    )  # fmt: skip


# --- Ingreso y salida ---------------------------------------------------------------------


@router.get("/", include_in_schema=False)
async def raiz() -> RedirectResponse:
    return RedirectResponse("/portal", status_code=303)


@router.get("/portal/entrar", response_class=HTMLResponse)
async def formulario_ingreso(request: Request):
    return _pagina(request, "entrar.html", {"error": None})


@router.post("/portal/entrar")
async def ingresar(
    request: Request,
    correo: str = Form(..., max_length=254),
    clave: str = Form(..., max_length=200),
    codigo: str = Form(..., max_length=10),
):
    _mismo_origen(request)
    bd, ip = request.app.state.bd, _ip(request)
    async with bd.como_anonimo() as con:
        cur = await con.execute(
            "SELECT usuario_id, totp_cifrado FROM idcmd.verificar_clave(%s, %s)",
            (correo.strip(), clave),
        )
        fila = await cur.fetchone()
        valido = False
        if fila and fila[1]:
            try:
                secreto = descifrar_secreto(fila[1], llave_desde_entorno())
                valido = verificar_totp(secreto, codigo)
            except InvalidTag:
                valido = False
            await con.execute(
                "SELECT idcmd.registrar_ingreso(%s)" if valido
                else "SELECT idcmd.registrar_fallo_mfa(%s)",
                (fila[0],),
            )  # fmt: skip
        if not valido:
            # El correo intentado queda en la auditoría, nunca la clave ni el código.
            await consultas.auditar(
                con, None, "ingreso_fallido", "/portal/entrar", ip, {"correo": correo.strip()[:254]}
            )
    if not valido:
        mensaje = (
            "Correo, clave o código incorrectos. Después de 5 intentos fallidos la cuenta se "
            "bloquea 15 minutos."
        )
        return _pagina(request, "entrar.html", {"error": mensaje}, estado=401)

    token = generar_token_sesion()
    async with bd.como_anonimo() as con:
        await con.execute(
            "SELECT idcmd.crear_sesion(%s, %s, %s, %s)",
            (hash_sesion(token), fila[0], DURACION_SESION, ip),
        )
    async with bd.como_usuario(fila[0]) as con:
        await consultas.auditar(con, fila[0], "ingreso", "/portal/entrar", ip)
    respuesta = RedirectResponse("/portal", status_code=303)
    respuesta.set_cookie(
        COOKIE,
        token,
        max_age=int(DURACION_SESION.total_seconds()),
        httponly=True,
        secure=_cookie_segura(),
        samesite="strict",
        path="/",
    )
    return respuesta


@router.post("/portal/salir")
async def salir(request: Request):
    _mismo_origen(request)
    token = request.cookies.get(COOKIE)
    usuario_id = await _usuario_de_sesion(request)
    if token:
        async with request.app.state.bd.como_anonimo() as con:
            await con.execute("SELECT idcmd.cerrar_sesion(%s)", (hash_sesion(token),))
    if usuario_id:
        async with request.app.state.bd.como_usuario(usuario_id) as con:
            await consultas.auditar(con, usuario_id, "salida", "/portal/salir", _ip(request))
    respuesta = _a_ingreso()
    respuesta.delete_cookie(COOKIE, path="/")
    return respuesta


# --- Páginas --------------------------------------------------------------------------------


@router.get("/portal", response_class=HTMLResponse)
async def inicio(request: Request):
    usuario_id = await _usuario_de_sesion(request)
    if usuario_id is None:
        return _a_ingreso()
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        usuario = await consultas.usuario(con)
        lista = await consultas.sitios(con)
        await consultas.auditar(con, usuario_id, "ver", "/portal", _ip(request))
    return _pagina(request, "inicio.html", {"usuario": usuario, "sitios": lista})


@router.get("/portal/sitios/{sitio_id}", response_class=HTMLResponse)
async def ver_sitio(request: Request, sitio_id: uuid.UUID):
    usuario_id = await _usuario_de_sesion(request)
    if usuario_id is None:
        return _a_ingreso()
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        usuario = await consultas.usuario(con)
        datos = await consultas.sitio(con, sitio_id)
        if datos is None:  # no existe o la RLS no lo deja ver: misma respuesta
            raise HTTPException(status_code=404)
        equipos = await consultas.equipos(con, sitio_id)
        await consultas.auditar(con, usuario_id, "ver", f"/portal/sitios/{sitio_id}", _ip(request))
    return _pagina(request, "sitio.html", {"usuario": usuario, "sitio": datos, "equipos": equipos})


@router.get("/portal/sitios/{sitio_id}/equipos", response_class=HTMLResponse)
async def fragmento_equipos(request: Request, sitio_id: uuid.UUID):
    """Refresco en vivo (HTMX cada 15 s). No se audita cada refresco: la vista ya lo está."""
    usuario_id = await _usuario_de_sesion(request)
    if usuario_id is None:
        # HTMX sigue la cabecera y lleva el navegador al ingreso cuando la sesión vence.
        return Response(status_code=401, headers={"HX-Redirect": "/portal/entrar"})
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        datos = await consultas.sitio(con, sitio_id)
        if datos is None:
            raise HTTPException(status_code=404)
        equipos = await consultas.equipos(con, sitio_id)
        tickets = await consultas.tickets_abiertos_por_equipo(con, sitio_id)
    return _pagina(
        request, "_equipos.html", {"sitio": datos, "equipos": equipos, "tickets": tickets}
    )


@router.get("/portal/sitios/{sitio_id}/auditoria", response_class=HTMLResponse)
async def ver_auditoria(request: Request, sitio_id: uuid.UUID):
    usuario_id = await _usuario_de_sesion(request)
    if usuario_id is None:
        return _a_ingreso()
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        usuario = await consultas.usuario(con)
        datos = await consultas.sitio(con, sitio_id)
        if datos is None:
            raise HTTPException(status_code=404)
        operaciones = await consultas.operaciones(con, sitio_id)
        cambios = await consultas.cambios(con, sitio_id)
        await consultas.auditar(
            con, usuario_id, "ver", f"/portal/sitios/{sitio_id}/auditoria", _ip(request)
        )
    return _pagina(
        request,
        "auditoria.html",
        {"usuario": usuario, "sitio": datos, "operaciones": operaciones, "cambios": cambios},
    )


# --- Servicio: tickets ------------------------------------------------------------------------


@router.get("/portal/tickets", response_class=HTMLResponse)
async def ver_tickets(
    request: Request, ver: str = Query("abiertos", pattern="^(abiertos|cerrados)$")
):
    usuario_id = await _usuario_de_sesion(request)
    if usuario_id is None:
        return _a_ingreso()
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        usuario = await consultas.usuario(con)
        lista = await consultas.tickets(con, abiertos=ver == "abiertos")
        await consultas.auditar(con, usuario_id, "ver", "/portal/tickets", _ip(request))
    return _pagina(request, "tickets.html", {"usuario": usuario, "tickets": lista, "ver": ver})


@router.get("/portal/tickets/{ticket_id}", response_class=HTMLResponse)
async def ver_ticket(request: Request, ticket_id: uuid.UUID, error: str | None = None):
    usuario_id = await _usuario_de_sesion(request)
    if usuario_id is None:
        return _a_ingreso()
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        usuario = await consultas.usuario(con)
        t = await consultas.ticket(con, ticket_id)
        if t is None:
            raise HTTPException(status_code=404)
        eventos = await consultas.eventos_ticket(con, ticket_id)
        pasos = await consultas.pasos_ticket(con, ticket_id)
        archivos = await consultas.archivos_ticket(con, ticket_id)
        await consultas.auditar(
            con, usuario_id, "ver", f"/portal/tickets/{ticket_id}", _ip(request)
        )
    puede_actuar = usuario["rol"] == "admin" or (
        usuario["rol"] == "tecnico"
        and (t["tecnico_id"] == usuario_id or t["estado"] in ("abierto", "asignado"))
    )
    # Trabajo en campo (llegada, pasos, fotos, firma): el asignado o un administrador.
    en_campo = t["estado"] in ("aceptado", "en_camino", "en_sitio") and (
        usuario["rol"] == "admin" or (usuario["rol"] == "tecnico" and t["tecnico_id"] == usuario_id)
    )
    return _pagina(
        request,
        "ticket.html",
        {
            "usuario": usuario, "t": t, "eventos": eventos, "puede_actuar": puede_actuar,
            "error": (error or "")[:200], "pasos": pasos, "archivos": archivos,
            "en_campo": en_campo, "pendientes": sum(p["estado"] == "pendiente" for p in pasos),
        },
    )  # fmt: skip


@router.post("/portal/tickets/{ticket_id}/accion")
async def accion_ticket(
    request: Request,
    ticket_id: uuid.UUID,
    accion: str = Form(..., pattern="^[a-z_]{1,20}$"),
    nota: str = Form("", max_length=1000),
):
    _mismo_origen(request)
    usuario_id = await _usuario_de_sesion(request)
    if usuario_id is None:
        return _a_ingreso()
    destino = f"/portal/tickets/{ticket_id}"
    try:
        async with request.app.state.bd.como_usuario(usuario_id) as con:
            # La base valida quién puede hacer qué (idcmd.accion_ticket) y deja el evento.
            await con.execute(
                "SELECT idcmd.accion_ticket(%s, %s, %s)", (ticket_id, accion, nota or None)
            )
    except psycopg.Error as e:
        mensaje = (e.diag.message_primary or "acción no permitida") if e.diag else "error"
        from urllib.parse import quote

        return RedirectResponse(f"{destino}?error={quote(mensaje)}", status_code=303)
    return RedirectResponse(destino, status_code=303)


@router.get("/portal/avisos", response_class=HTMLResponse)
async def ver_avisos(request: Request):
    usuario_id = await _usuario_de_sesion(request)
    if usuario_id is None:
        return _a_ingreso()
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        usuario = await consultas.usuario(con)
        if usuario["rol"] != "admin":
            raise HTTPException(status_code=404)
        lista = await consultas.avisos_enviados(con)
    return _pagina(request, "avisos.html", {"usuario": usuario, "avisos": lista})


# --- Seguimiento del cliente (sin sesión, enlace firmado) ---------------------------------------


@router.get("/seguimiento/{ticket_id}/{firma}", response_class=HTMLResponse)
async def seguimiento(request: Request, ticket_id: uuid.UUID, firma: str):
    if len(firma) != 32 or not firma_valida(ticket_id, firma):
        raise HTTPException(status_code=404)
    async with request.app.state.bd.como_anonimo() as con:
        cur = await con.execute("SELECT * FROM idcmd.seguimiento_ticket(%s)", (ticket_id,))
        fila = await cur.fetchone()
        nombres = [c.name for c in cur.description] if cur.description else []
        if fila:
            await consultas.auditar(
                con, None, "seguimiento", f"/seguimiento/{ticket_id}", _ip(request)
            )
    if not fila:
        raise HTTPException(status_code=404)
    return _pagina(
        request,
        "seguimiento.html",
        {
            "t": dict(zip(nombres, fila, strict=True)),
            "acta": f"/seguimiento/{ticket_id}/{firma}/acta",
        },
    )
