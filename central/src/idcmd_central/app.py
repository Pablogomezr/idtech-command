"""Aplicación FastAPI de la central."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__
from .db import BaseDatos
from .ingesta.api import router as router_ingesta


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

    @app.get("/salud")
    async def salud() -> dict:
        return {"estado": "ok", "version": __version__}

    return app
