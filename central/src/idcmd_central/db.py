"""Acceso a Postgres. Cada operación corre en una transacción con el rol y la identidad
que correspondan, para que la RLS aplique siempre:

    async with bd.como_usuario(usuario_id) as con:   # portal (rol idcmd_app)
    async with bd.como_agente(agente_id) as con:     # ingesta (rol idcmd_ingesta)

El usuario de login de la central es miembro NOINHERIT de ambos roles: sin SET ROLE no
tiene permisos sobre nada.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool


@dataclass(frozen=True)
class AgenteAutenticado:
    agente_id: uuid.UUID
    cliente_id: uuid.UUID
    sitio_id: uuid.UUID


class BaseDatos:
    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 10):
        self.pool = AsyncConnectionPool(
            dsn, min_size=min_size, max_size=max_size, open=False, kwargs={"autocommit": True}
        )

    async def abrir(self) -> None:
        await self.pool.open(wait=True)

    async def cerrar(self) -> None:
        await self.pool.close()

    @asynccontextmanager
    async def _transaccion(
        self, rol: str, ajuste: str, valor: str
    ) -> AsyncIterator[AsyncConnection]:
        async with self.pool.connection() as con, con.transaction():
            # Los nombres de rol y ajuste son constantes de este módulo, no datos externos.
            await con.execute(f"SET LOCAL ROLE {rol}")
            await con.execute("SELECT set_config(%s, %s, true)", (ajuste, valor))
            yield con

    def como_usuario(self, usuario_id: uuid.UUID):
        return self._transaccion("idcmd_app", "app.usuario_id", str(usuario_id))

    def como_anonimo(self):
        """Portal sin sesión (ingreso): rol idcmd_app sin usuario; la RLS no muestra nada y
        solo sirven las funciones de ingreso y sesión."""
        return self._transaccion("idcmd_app", "app.usuario_id", "")

    def como_worker(self):
        """Worker de servicio: ve todos los sitios, pero solo en las tablas de tickets y lo que
        necesita para abrirlos (rol idcmd_worker, sin BYPASSRLS)."""
        return self._transaccion("idcmd_worker", "app.usuario_id", "")

    def como_agente(self, agente_id: uuid.UUID):
        return self._transaccion("idcmd_ingesta", "app.agente_id", str(agente_id))

    async def autenticar_agente(self, token_hash: bytes) -> AgenteAutenticado | None:
        async with self.pool.connection() as con, con.transaction():
            await con.execute("SET LOCAL ROLE idcmd_ingesta")
            cur = await con.execute(
                "SELECT agente_id, cliente_id, sitio_id FROM idcmd.autenticar_agente(%s)",
                (token_hash,),
            )
            fila = await cur.fetchone()
        return AgenteAutenticado(*fila) if fila else None
