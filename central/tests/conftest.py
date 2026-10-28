"""Infraestructura de pruebas: Postgres real (no simulado), base nueva por sesión.

Requiere un Postgres 16 accesible en IDCMD_PRUEBAS_DSN con un rol que pueda crear bases y
roles. Localmente:

    docker run -d --name idcmd-pg -e POSTGRES_PASSWORD=pruebas -p 55432:5432 postgres:16
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import sql

from idcmd_central.db import BaseDatos
from idcmd_central.migrar import migrar
from idcmd_central.tokens import generar_token, hash_token

DSN_ADMIN = os.environ.get(
    "IDCMD_PRUEBAS_DSN", "postgresql://postgres:pruebas@localhost:55432/postgres"
)
ROL_SERVICIO = "idcmd_servicio_pruebas"
CLAVE_SERVICIO = "clave-solo-para-pruebas"


def _dsn(base: str, usuario: str | None = None, clave: str | None = None) -> str:
    info = psycopg.conninfo.conninfo_to_dict(DSN_ADMIN)
    info["dbname"] = base
    if usuario:
        info["user"], info["password"] = usuario, clave
    return psycopg.conninfo.make_conninfo(**info)


@pytest.fixture(scope="session")
def base_pruebas():
    nombre = f"idcmd_pruebas_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(DSN_ADMIN, autocommit=True) as con:
        con.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(nombre)))
    dsn_dueno = _dsn(nombre)
    migrar(dsn_dueno)
    with psycopg.connect(dsn_dueno, autocommit=True) as con:
        existe = con.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (ROL_SERVICIO,)
        ).fetchone()
        if not existe:
            con.execute(
                sql.SQL("CREATE ROLE {} LOGIN NOINHERIT PASSWORD {}").format(
                    sql.Identifier(ROL_SERVICIO), sql.Literal(CLAVE_SERVICIO)
                )
            )
        con.execute(
            sql.SQL("GRANT idcmd_app, idcmd_ingesta, idcmd_worker TO {}").format(
                sql.Identifier(ROL_SERVICIO)
            )
        )
    yield {"dueno": dsn_dueno, "servicio": _dsn(nombre, ROL_SERVICIO, CLAVE_SERVICIO)}
    with psycopg.connect(DSN_ADMIN, autocommit=True) as con:
        con.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(nombre)))


@dataclass
class Datos:
    """Escenario: dos clientes; el A con dos regionales y tres sitios, el B con un sitio."""

    ids: dict[str, uuid.UUID] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)

    def __getitem__(self, clave: str) -> uuid.UUID:
        return self.ids[clave]


def _sembrar(con: psycopg.Connection) -> Datos:
    d = Datos()

    def nuevo(clave: str) -> uuid.UUID:
        d.ids[clave] = uuid.uuid4()
        return d.ids[clave]

    con.execute(
        "INSERT INTO idcmd.clientes (id, nombre) VALUES (%s, 'Integradora A'),"
        " (%s, 'Integradora B')",
        (nuevo("cliente_a"), nuevo("cliente_b")),
    )
    for reg, cli in [
        ("reg_a_norte", "cliente_a"),
        ("reg_a_sur", "cliente_a"),
        ("reg_b", "cliente_b"),
    ]:
        con.execute(
            "INSERT INTO idcmd.regionales (id, cliente_id, nombre) VALUES (%s, %s, %s)",
            (nuevo(reg), d[cli], reg),
        )
    for sitio, reg, cli in [
        ("sitio_a1", "reg_a_norte", "cliente_a"),
        ("sitio_a2", "reg_a_norte", "cliente_a"),
        ("sitio_a3", "reg_a_sur", "cliente_a"),
        ("sitio_b1", "reg_b", "cliente_b"),
    ]:
        con.execute(
            "INSERT INTO idcmd.sitios (id, cliente_id, regional_id, nombre)"
            " VALUES (%s, %s, %s, %s)",
            (nuevo(sitio), d[cli], d[reg], sitio),
        )
        con.execute(
            "INSERT INTO idcmd.dispositivos (id, cliente_id, sitio_id, nombre, tipo_equipo,"
            " marca, direccion, retencion_contratada_dias, credencial_ref)"
            " VALUES (%s, %s, %s, %s, 'camara', 'hikvision', '10.0.0.10', 30, 'nvr-local')",
            (nuevo(f"camara_{sitio}"), d[cli], d[sitio], f"Cámara {sitio}"),
        )
        token = generar_token()
        d.tokens[sitio] = token
        con.execute(
            "INSERT INTO idcmd.agentes (id, cliente_id, sitio_id, nombre, token_hash)"
            " VALUES (%s, %s, %s, %s, %s)",
            (nuevo(f"agente_{sitio}"), d[cli], d[sitio], f"Mini PC {sitio}", hash_token(token)),
        )
        con.execute(
            "INSERT INTO idcmd.eventos_estado (id, dispositivo_id, cliente_id, sitio_id,"
            " agente_id, chequeo, estado_anterior, estado_nuevo, observado_at)"
            " VALUES (%s, %s, %s, %s, %s, 'icmp', 'ok', 'caido', now())",
            (uuid.uuid4(), d[f"camara_{sitio}"], d[cli], d[sitio], d[f"agente_{sitio}"]),
        )

    usuarios = [
        ("admin", "admin"),
        ("tecnico_1", "tecnico"),
        ("tecnico_vencido", "tecnico"),
        ("cliente_a", "cliente"),
        ("cliente_a_norte", "cliente"),
        ("cliente_a3", "cliente"),
        ("cliente_b", "cliente"),
        ("cliente_inactivo", "cliente"),
    ]
    for clave, rol in usuarios:
        con.execute(
            "INSERT INTO idcmd.usuarios (id, correo, nombre, rol, activo)"
            " VALUES (%s, %s, %s, %s, %s)",
            (nuevo(f"u_{clave}"), f"{clave}@prueba.co", clave, rol, clave != "cliente_inactivo"),
        )

    alcances = [
        ("u_cliente_a", "cliente_a", None, None),
        ("u_cliente_a_norte", "cliente_a", "reg_a_norte", None),
        ("u_cliente_a3", "cliente_a", None, "sitio_a3"),
        ("u_cliente_b", "cliente_b", None, None),
        ("u_cliente_inactivo", "cliente_a", None, None),
    ]
    for usuario, cli, reg, sitio in alcances:
        con.execute(
            "INSERT INTO idcmd.alcances_usuario (usuario_id, cliente_id, regional_id,"
            " sitio_id) VALUES (%s, %s, %s, %s)",
            (
                d[usuario],
                d[cli],
                d.ids.get(reg) if reg else None,
                d.ids.get(sitio) if sitio else None,
            ),
        )

    ahora = datetime.now(UTC)
    con.execute(
        "INSERT INTO idcmd.asignaciones_tecnico (tecnico_id, cliente_id, sitio_id)"
        " VALUES (%s, %s, %s)",
        (d["u_tecnico_1"], d["cliente_a"], d["sitio_a1"]),
    )
    con.execute(
        "INSERT INTO idcmd.asignaciones_tecnico (tecnico_id, cliente_id, sitio_id, tipo,"
        " vigente_desde, vigente_hasta) VALUES (%s, %s, %s, 'suplente', %s, %s)",
        (
            d["u_tecnico_vencido"],
            d["cliente_a"],
            d["sitio_a2"],
            ahora - timedelta(days=30),
            ahora - timedelta(days=1),
        ),
    )
    return d


@pytest.fixture
def datos(base_pruebas) -> Datos:
    with psycopg.connect(base_pruebas["dueno"], autocommit=True) as con:
        tablas = con.execute(
            "SELECT string_agg(format('idcmd.%I', tablename), ', ')"
            " FROM pg_tables WHERE schemaname = 'idcmd'"
        ).fetchone()[0]
        con.execute(f"TRUNCATE {tablas} CASCADE")
        with con.transaction():
            return _sembrar(con)


@pytest.fixture
async def bd(base_pruebas):
    base = BaseDatos(base_pruebas["servicio"], min_size=1, max_size=4)
    await base.abrir()
    yield base
    await base.cerrar()


@pytest.fixture
def dueno(base_pruebas):
    with psycopg.connect(base_pruebas["dueno"], autocommit=True) as con:
        yield con
