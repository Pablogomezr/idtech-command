"""Consola de operación de la central (`idcmd-central`). Usa IDCMD_DSN_ADMIN (rol dueño).

idcmd-central migrar
idcmd-central agentes crear --sitio <uuid> --nombre "Mini PC sede norte"
idcmd-central agentes revocar <agente_id>
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid

import psycopg

from .migrar import migrar
from .tokens import generar_token, hash_token


def _dsn_admin() -> str:
    try:
        return os.environ["IDCMD_DSN_ADMIN"]
    except KeyError:
        sys.exit("defina IDCMD_DSN_ADMIN (conexión con el rol dueño del esquema)")


def cmd_migrar(args) -> int:
    aplicadas = migrar(_dsn_admin())
    print("migraciones aplicadas: " + (", ".join(aplicadas) or "ninguna (al día)"))
    return 0


def crear_agente(dsn: str, sitio_id: uuid.UUID, nombre: str) -> tuple[uuid.UUID, str]:
    token = generar_token()
    with psycopg.connect(dsn) as con:
        fila = con.execute(
            "INSERT INTO idcmd.agentes (cliente_id, sitio_id, nombre, token_hash)"
            " SELECT cliente_id, id, %s, %s FROM idcmd.sitios WHERE id = %s RETURNING id",
            (nombre, hash_token(token), sitio_id),
        ).fetchone()
    if fila is None:
        raise LookupError(f"no existe el sitio {sitio_id}")
    return fila[0], token


def cmd_agentes_crear(args) -> int:
    agente_id, token = crear_agente(_dsn_admin(), args.sitio, args.nombre)
    print(f"agente_id: {agente_id}")
    print(f"token:     {token}")
    print("El token se muestra UNA sola vez. Cópielo al archivo de secreto del mini PC.")
    return 0


def cmd_agentes_revocar(args) -> int:
    with psycopg.connect(_dsn_admin()) as con:
        n = con.execute(
            "UPDATE idcmd.agentes SET revocado_at = now() WHERE id = %s AND revocado_at IS NULL",
            (args.agente_id,),
        ).rowcount
    print("agente revocado" if n else "no existe o ya estaba revocado")
    return 0 if n else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="idcmd-central")
    sub = p.add_subparsers(dest="comando", required=True)
    sub.add_parser("migrar").set_defaults(fn=cmd_migrar)
    agentes = sub.add_parser("agentes").add_subparsers(dest="accion", required=True)
    crear = agentes.add_parser("crear")
    crear.add_argument("--sitio", type=uuid.UUID, required=True)
    crear.add_argument("--nombre", required=True)
    crear.set_defaults(fn=cmd_agentes_crear)
    revocar = agentes.add_parser("revocar")
    revocar.add_argument("agente_id", type=uuid.UUID)
    revocar.set_defaults(fn=cmd_agentes_revocar)
    args = p.parse_args(argv)
    return args.fn(args)
