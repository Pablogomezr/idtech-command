"""Aplicación FastAPI de la central."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from . import __version__
from .db import BaseDatos
from .ingesta.api import router as router_ingesta
from .portal.admin import router as router_admin
from .portal.campo import router as router_campo
from .portal.gestion import router as router_gestion
from .portal.informes import router as router_informes
from .portal.modelos import router as router_modelos
from .portal.rutas import router as router_portal
from .portal.servicios import router as router_servicios

# El portal no carga nada de terceros: htmx y la hoja de estilos se sirven desde aquí.
CABECERAS_PORTAL = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'; worker-src 'self'; manifest-src 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


def crear_app(bd: BaseDatos | None = None) -> FastAPI:
    """Con `bd` se usa una base ya abierta (pruebas); si no, se abre desde IDCMD_DSN."""

    @asynccontextmanager
    async def ciclo_de_vida(app: FastAPI):
        if bd is not None:
            app.state.bd = bd
            yield
            return
        app.state.bd = BaseDatos(os.environ["IDCMD_DSN"])
        await app.state.bd.abrir()
        try:
            yield
        finally:
            await app.state.bd.cerrar()

    app = FastAPI(
        title="IDtech Command — central",
        version=__version__,
        lifespan=ciclo_de_vida,
        docs_url=None,  # sin documentación pública de la API
        redoc_url=None,
        openapi_url=None,
    )
    if bd is not None:
        app.state.bd = bd
    app.include_router(router_ingesta)
    app.include_router(router_portal)
    app.include_router(router_campo)
    app.include_router(router_informes)
    app.include_router(router_servicios)
    app.include_router(router_admin)
    app.include_router(router_gestion)
    app.include_router(router_modelos)
    app.mount(
        "/portal/estaticos",
        StaticFiles(directory=Path(__file__).parent / "portal" / "estaticos"),
        name="estaticos",
    )

    @app.middleware("http")
    async def cabeceras_seguridad(request: Request, llamar):
        respuesta = await llamar(request)
        ruta = request.url.path
        if ruta == "/" or ruta.startswith(("/portal", "/seguimiento", "/informe")):
            for nombre, valor in CABECERAS_PORTAL.items():
                if nombre == "Cache-Control" and "/estaticos/" in request.url.path:
                    continue
                respuesta.headers.setdefault(nombre, valor)
        return respuesta

    @app.get("/salud")
    async def salud() -> dict:
        return {"estado": "ok", "version": __version__}

    return app
