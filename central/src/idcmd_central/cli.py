"""Consola de operación de la central (`idcmd-central`). Usa IDCMD_DSN_ADMIN (rol dueño).

idcmd-central migrar
idcmd-central agentes crear --sitio <uuid> --nombre "Mini PC sede norte"
idcmd-central agentes revocar <agente_id>
idcmd-central llave-totp                       # llave para cifrar los segundos factores
idcmd-central usuarios crear --correo a@b.co --nombre "Pablo" --rol admin
idcmd-central laboratorio preparar --inventario oficina.yaml --salida /lab
idcmd-central usuarios telegram --correo a@b.co --chat-id 123456789
idcmd-central worker                          # tickets, escalamiento, SLA y avisos
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import uuid
from pathlib import Path

import psycopg
import yaml
from psycopg import sql

from . import laboratorio
from .migrar import migrar
from .portal.seguridad import (
    cifrar_secreto,
    generar_llave,
    generar_secreto,
    llave_desde_entorno,
    uri_totp,
)
from .tokens import generar_token, hash_token

LONGITUD_MINIMA_CLAVE = 12


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


def cmd_worker(args) -> int:
    import asyncio
    import logging

    from .db import BaseDatos
    from .servicio.worker import ejecutar

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    async def principal():
        bd = BaseDatos(os.environ["IDCMD_DSN"], max_size=4)
        await bd.abrir()
        try:
            await ejecutar(bd)
        finally:
            await bd.cerrar()

    asyncio.run(principal())
    return 0


def cmd_usuarios_telegram(args) -> int:
    with psycopg.connect(_dsn_admin()) as con:
        n = con.execute(
            "UPDATE idcmd.usuarios SET telegram_chat_id = %s WHERE correo = %s",
            (args.chat_id, args.correo),
        ).rowcount
    if not n:
        sys.exit(f"no existe el usuario {args.correo}")
    print(f"Telegram vinculado a {args.correo}")
    return 0


def cmd_llave_totp(args) -> int:
    print(generar_llave())
    print("Guárdela como IDCMD_LLAVE_TOTP (secreto del servidor). Si se pierde, todos los")
    print("usuarios deben volver a registrar su segundo factor.", file=sys.stderr)
    return 0


def crear_usuario(
    dsn: str, correo: str, nombre: str, rol: str, clave: str
) -> tuple[uuid.UUID, str]:
    """Crea el usuario con clave (bcrypt en la base) y segundo factor obligatorio."""
    secreto = generar_secreto()
    cifrado = cifrar_secreto(secreto, llave_desde_entorno())
    with psycopg.connect(dsn) as con:
        fila = con.execute(
            "INSERT INTO idcmd.usuarios (correo, nombre, rol, hash_clave, totp_cifrado)"
            " VALUES (%s, %s, %s, crypt(%s, gen_salt('bf', 12)), %s) RETURNING id",
            (correo, nombre, rol, clave, cifrado),
        ).fetchone()
    return fila[0], secreto


def restablecer_mfa(dsn: str, correo: str, clave: str | None = None) -> str:
    """Nuevo segundo factor (celular perdido o clave de configuración no guardada). Cierra
    las sesiones abiertas y quita el bloqueo; con `clave`, también cambia la clave."""
    secreto = generar_secreto()
    cifrado = cifrar_secreto(secreto, llave_desde_entorno())
    with psycopg.connect(dsn) as con, con.transaction():
        fila = con.execute(
            "UPDATE idcmd.usuarios SET totp_cifrado = %s, intentos_fallidos = 0,"
            " bloqueado_hasta = NULL,"
            " hash_clave = coalesce(crypt(%s, gen_salt('bf', 12)), hash_clave)"
            " WHERE correo = %s RETURNING id",
            (cifrado, clave, correo),
        ).fetchone()
        if fila is None:
            raise LookupError(f"no existe el usuario {correo}")
        con.execute(
            "UPDATE idcmd.sesiones SET cerrada_at = now()"
            " WHERE usuario_id = %s AND cerrada_at IS NULL",
            (fila[0],),
        )
    return secreto


def _pedir_clave() -> str:
    clave = getpass.getpass("Clave del portal (mínimo 12 caracteres): ")
    if len(clave) < LONGITUD_MINIMA_CLAVE:
        sys.exit("la clave debe tener al menos 12 caracteres")
    if getpass.getpass("Repita la clave: ") != clave:
        sys.exit("las claves no coinciden")
    return clave


def _mostrar_segundo_factor(correo: str, secreto: str) -> None:
    print()
    print("Segundo factor: en Google o Microsoft Authenticator, 'Agregar cuenta' ->")
    print("'Ingresar una clave de configuración' (o escanee la URI):")
    print(f"  cuenta: {correo}")
    print(f"  clave:  {secreto}")
    print(f"  URI:    {uri_totp(secreto, correo)}")
    print("Esta clave se muestra UNA sola vez. Agréguela al celular antes de cerrar esta ventana.")


def cmd_usuarios_restablecer_mfa(args) -> int:
    clave = _pedir_clave() if args.nueva_clave else None
    try:
        secreto = restablecer_mfa(_dsn_admin(), args.correo, clave)
    except LookupError as e:
        sys.exit(str(e))
    print(f"segundo factor restablecido para {args.correo}; sesiones abiertas cerradas")
    _mostrar_segundo_factor(args.correo, secreto)
    return 0


def cmd_usuarios_crear(args) -> int:
    clave = _pedir_clave()
    try:
        usuario_id, secreto = crear_usuario(_dsn_admin(), args.correo, args.nombre, args.rol, clave)
    except psycopg.errors.UniqueViolation:
        sys.exit(
            f"ya existe un usuario con el correo {args.correo}. Si perdió el segundo factor:"
            f" idcmd-central usuarios restablecer-mfa --correo {args.correo}"
        )
    print(f"usuario creado: {usuario_id} ({args.rol})")
    _mostrar_segundo_factor(args.correo, secreto)
    return 0


def cmd_laboratorio_preparar(args) -> int:
    """Base al día, rol de servicio de la aplicación, inventario, agente y su config."""
    dsn = _dsn_admin()
    aplicadas = migrar(dsn)
    print("migraciones: " + (", ".join(aplicadas) or "al día"))

    clave_servicio = os.environ.get("IDCMD_CLAVE_SERVICIO")
    if clave_servicio:
        with psycopg.connect(dsn, autocommit=True) as con:
            existe = con.execute("SELECT 1 FROM pg_roles WHERE rolname = 'idcmd_servicio'")
            orden = "ALTER" if existe.fetchone() else "CREATE"
            con.execute(
                sql.SQL(orden + " ROLE idcmd_servicio LOGIN NOINHERIT PASSWORD {}").format(
                    sql.Literal(clave_servicio)
                )
            )
            con.execute("GRANT idcmd_app, idcmd_ingesta, idcmd_worker TO idcmd_servicio")
        print("rol de servicio idcmd_servicio: listo")

    datos = laboratorio.leer(args.inventario)
    resultado = laboratorio.cargar(dsn, datos)
    print(f"sitio '{datos['sitio']}': {resultado.sitio_id} ({len(resultado.dispositivos)} equipos)")

    salida: Path = args.salida
    salida.mkdir(parents=True, exist_ok=True)
    archivo_config, archivo_token = salida / "agente.yaml", salida / "token_agente"
    agente_id = None
    if archivo_config.exists() and archivo_token.exists():
        previo = yaml.safe_load(archivo_config.read_text(encoding="utf-8")) or {}
        agente_id = uuid.UUID(previo["agente_id"]) if previo.get("agente_id") else None
    if agente_id is None:
        agente_id, token = crear_agente(dsn, resultado.sitio_id, "Agente de laboratorio")
        archivo_token.write_text(token + "\n", encoding="utf-8")
        print(f"agente creado: {agente_id} (token en {archivo_token})")
    config = laboratorio.config_agente(datos, resultado, agente_id, args.url_central)
    archivo_config.write_text(
        "# Generado por 'idcmd-central laboratorio preparar'. No lleva claves.\n"
        + yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"configuración del agente: {archivo_config} ({len(config['objetivos'])} objetivos)")
    return 0


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
    sub.add_parser("llave-totp").set_defaults(fn=cmd_llave_totp)
    usuarios = sub.add_parser("usuarios").add_subparsers(dest="accion", required=True)
    u_crear = usuarios.add_parser("crear")
    u_crear.add_argument("--correo", required=True)
    u_crear.add_argument("--nombre", required=True)
    u_crear.add_argument("--rol", choices=["admin", "tecnico", "cliente"], required=True)
    u_crear.set_defaults(fn=cmd_usuarios_crear)
    u_mfa = usuarios.add_parser("restablecer-mfa", help="nuevo segundo factor para un usuario")
    u_mfa.add_argument("--correo", required=True)
    u_mfa.add_argument("--nueva-clave", action="store_true", help="cambiar también la clave")
    u_mfa.set_defaults(fn=cmd_usuarios_restablecer_mfa)
    u_tg = usuarios.add_parser("telegram", help="vincular el chat de Telegram de un usuario")
    u_tg.add_argument("--correo", required=True)
    u_tg.add_argument("--chat-id", type=int, required=True)
    u_tg.set_defaults(fn=cmd_usuarios_telegram)
    sub.add_parser("worker", help="servicio: tickets, escalamiento, SLA y avisos").set_defaults(
        fn=cmd_worker
    )
    lab = sub.add_parser("laboratorio").add_subparsers(dest="accion", required=True)
    preparar = lab.add_parser("preparar")
    preparar.add_argument("--inventario", type=Path, required=True)
    preparar.add_argument("--salida", type=Path, required=True)
    preparar.add_argument("--url-central", default="http://localhost:8000")
    preparar.set_defaults(fn=cmd_laboratorio_preparar)
    args = p.parse_args(argv)
    return args.fn(args)
