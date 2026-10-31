"""Consola de operación de la central (`idcmd-central`). Usa IDCMD_DSN_ADMIN (rol dueño).

idcmd-central migrar
idcmd-central agentes crear --sitio <uuid> --nombre "Mini PC sede norte"
idcmd-central agentes revocar <agente_id>
idcmd-central llave-totp                       # llave para cifrar los segundos factores
idcmd-central usuarios crear --correo a@b.co --nombre "Pablo" --rol admin
idcmd-central preparar                         # producción: migraciones + rol de servicio
idcmd-central inventario cargar --inventario sitio.yaml --salida /agente --url-central https://...
idcmd-central laboratorio preparar --inventario oficina.yaml --salida /lab
idcmd-central usuarios telegram --correo a@b.co --chat-id 123456789
idcmd-central worker                          # tickets, escalamiento, SLA y avisos
idcmd-central sitios                          # sitios con su id
idcmd-central exclusiones crear --sitio "Oficina" --motivo mantenimiento \
    --desde "2026-11-09 08:00" --hasta "2026-11-09 11:00" --descripcion "Cambio de UPS"
idcmd-central clientes sla --cliente "IDtech" --meta 99.5 --pdf-adjunto si
idcmd-central informe emitir --mes 2026-10 [--sitio "Oficina"] [--recalcular]
idcmd-central informe pdf --sitio "Oficina" --mes 2026-10 --salida informe.pdf
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import secrets
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


# Secuencias de teclas especiales (F1-F12, flechas...) que la terminal mete en lo escrito: el
# navegador no las inserta en el campo de clave, así que aquí tampoco deben contar.
_TECLAS_ESPECIALES = re.compile(r"\x1b(\[[0-9;?]*[ -/]*[@-~]|O.|.)|[\x00-\x1f\x7f]")
# Sin caracteres que se confunden (0/O, 1/l/I) para que se pueda copiar o dictar sin errores.
_ALFABETO_CLAVE = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def limpiar_clave(texto: str) -> str:
    return _TECLAS_ESPECIALES.sub("", texto)


def generar_clave() -> str:
    """Clave aleatoria de 4 grupos de 5 (~114 bits), p. ej. Kq7mX-p2t9V-dRhL4-w8Nc3."""
    return "-".join("".join(secrets.choice(_ALFABETO_CLAVE) for _ in range(5)) for _ in range(4))


def _pedir_clave() -> str:
    clave = limpiar_clave(getpass.getpass("Clave del portal (mínimo 12 caracteres): "))
    if len(clave) < LONGITUD_MINIMA_CLAVE:
        sys.exit("la clave debe tener al menos 12 caracteres")
    if limpiar_clave(getpass.getpass("Repita la clave: ")) != clave:
        sys.exit(
            "las claves no coinciden. Si su teclado mete teclas especiales, use --generar-clave"
        )
    return clave


def _clave_de(args) -> str:
    if getattr(args, "generar_clave", False):
        clave = generar_clave()
        print(f"Clave nueva del portal: {clave}")
        print("Cópiela YA a su gestor de claves: no se vuelve a mostrar.")
        return clave
    return _pedir_clave()


def _mostrar_segundo_factor(correo: str, secreto: str) -> None:
    print()
    print("Segundo factor: en Google o Microsoft Authenticator, 'Agregar cuenta' ->")
    print("'Ingresar una clave de configuración' (o escanee la URI):")
    print(f"  cuenta: {correo}")
    print(f"  clave:  {' '.join(secreto[i : i + 4] for i in range(0, len(secreto), 4))}")
    print(f"  URI:    {uri_totp(secreto, correo)}")
    print("Esta clave se muestra UNA sola vez. Agréguela al celular antes de cerrar esta ventana.")


def cmd_usuarios_restablecer_mfa(args) -> int:
    clave = _clave_de(args) if args.nueva_clave or args.generar_clave else None
    try:
        secreto = restablecer_mfa(_dsn_admin(), args.correo, clave)
    except LookupError as e:
        sys.exit(str(e))
    print(f"segundo factor restablecido para {args.correo}; sesiones abiertas cerradas")
    _mostrar_segundo_factor(args.correo, secreto)
    return 0


def cmd_usuarios_crear(args) -> int:
    clave = _clave_de(args)
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


def asegurar_rol_servicio(dsn: str, clave: str) -> None:
    """Rol de login de la aplicación: NOINHERIT, solo actúa tras SET LOCAL ROLE (ver db.py)."""
    with psycopg.connect(dsn, autocommit=True) as con:
        existe = con.execute("SELECT 1 FROM pg_roles WHERE rolname = 'idcmd_servicio'")
        orden = "ALTER" if existe.fetchone() else "CREATE"
        con.execute(
            sql.SQL(orden + " ROLE idcmd_servicio LOGIN NOINHERIT PASSWORD {}").format(
                sql.Literal(clave)
            )
        )
        con.execute("GRANT idcmd_app, idcmd_ingesta, idcmd_worker TO idcmd_servicio")


def _migrar_y_rol(dsn: str) -> None:
    aplicadas = migrar(dsn)
    print("migraciones: " + (", ".join(aplicadas) or "al día"))
    if clave_servicio := os.environ.get("IDCMD_CLAVE_SERVICIO"):
        asegurar_rol_servicio(dsn, clave_servicio)
        print("rol de servicio idcmd_servicio: listo")


def cmd_preparar(args) -> int:
    """Producción: base al día y rol de la aplicación. Se puede repetir en cada actualización."""
    if not os.environ.get("IDCMD_CLAVE_SERVICIO"):
        sys.exit("defina IDCMD_CLAVE_SERVICIO (clave del rol de login de la aplicación)")
    _migrar_y_rol(_dsn_admin())
    return 0


def cmd_inventario_cargar(args) -> int:
    """Sitio y equipos desde el YAML, agente del sitio y su configuración (sin claves)."""
    dsn = _dsn_admin()
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
        agente_id, token = crear_agente(dsn, resultado.sitio_id, f"Agente {datos['sitio']}")
        archivo_token.write_text(token + "\n", encoding="utf-8")
        print(f"agente creado: {agente_id} (token en {archivo_token})")
    config = laboratorio.config_agente(datos, resultado, agente_id, args.url_central)
    archivo_config.write_text(
        "# Generado por 'idcmd-central inventario cargar'. No lleva claves.\n"
        + yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"configuración del agente: {archivo_config} ({len(config['objetivos'])} objetivos)")
    return 0


def cmd_laboratorio_preparar(args) -> int:
    """Todo junto para un PC: base al día, rol de servicio, inventario, agente y su config."""
    _migrar_y_rol(_dsn_admin())
    return cmd_inventario_cargar(args)


# --- Informes de SLA -------------------------------------------------------------------------


def _sitio(con, texto: str) -> tuple[uuid.UUID, str]:
    """Sitio por id o por nombre exacto (sin distinguir mayúsculas)."""
    try:
        fila = con.execute(
            "SELECT id, nombre FROM idcmd.sitios WHERE id = %s", (uuid.UUID(texto),)
        ).fetchone()
    except ValueError:
        filas = con.execute(
            "SELECT id, nombre FROM idcmd.sitios WHERE lower(nombre) = lower(%s)", (texto,)
        ).fetchall()
        if len(filas) > 1:
            sys.exit(
                f"hay {len(filas)} sitios llamados '{texto}': use el id (idcmd-central sitios)"
            )
        fila = filas[0] if filas else None
    if fila is None:
        sys.exit(f"no existe el sitio '{texto}' (idcmd-central sitios)")
    return fila[0], fila[1]


def _hora_local(texto: str):
    from datetime import datetime

    from .portal.presentacion import ZONA

    try:
        return datetime.strptime(texto, "%Y-%m-%d %H:%M").replace(tzinfo=ZONA)
    except ValueError:
        sys.exit(f"fecha no válida '{texto}': use AAAA-MM-DD HH:MM (hora de Colombia)")


def cmd_sitios(args) -> int:
    with psycopg.connect(_dsn_admin()) as con:
        for sitio_id, nombre, cliente in con.execute(
            "SELECT s.id, s.nombre, c.nombre FROM idcmd.sitios s"
            " JOIN idcmd.clientes c ON c.id = s.cliente_id ORDER BY c.nombre, s.nombre"
        ):
            print(f"{sitio_id}  {nombre}  ({cliente})")
    return 0


def cmd_exclusiones_crear(args) -> int:
    desde, hasta = _hora_local(args.desde), _hora_local(args.hasta)
    if hasta <= desde:
        sys.exit("--hasta debe ser posterior a --desde")
    with psycopg.connect(_dsn_admin()) as con:
        sitio_id, nombre = _sitio(con, args.sitio)
        equipo = None
        if args.equipo:
            fila = con.execute(
                "SELECT id FROM idcmd.dispositivos WHERE sitio_id = %s"
                " AND (id::text = %s OR lower(nombre) = lower(%s))",
                (sitio_id, args.equipo, args.equipo),
            ).fetchone()
            if fila is None:
                sys.exit(f"no existe el equipo '{args.equipo}' en {nombre}")
            equipo = fila[0]
        con.execute(
            "INSERT INTO idcmd.exclusiones_sla (cliente_id, sitio_id, dispositivo_id, motivo,"
            " desde, hasta, descripcion) SELECT cliente_id, id, %s, %s, %s, %s, %s"
            " FROM idcmd.sitios WHERE id = %s",
            (equipo, args.motivo, desde, hasta, args.descripcion, sitio_id),
        )
    print(f"exclusión registrada en {nombre}: {args.desde} a {args.hasta} ({args.motivo})")
    print("Si el informe de ese mes ya se emitió: idcmd-central informe emitir --recalcular")
    return 0


def cmd_exclusiones_listar(args) -> int:
    from .portal.presentacion import hora

    with psycopg.connect(_dsn_admin()) as con:
        sitio_id, _ = _sitio(con, args.sitio)
        for motivo, desde, hasta, desc, equipo in con.execute(
            "SELECT e.motivo, e.desde, e.hasta, e.descripcion, d.nombre"
            " FROM idcmd.exclusiones_sla e LEFT JOIN idcmd.dispositivos d"
            " ON d.id = e.dispositivo_id WHERE e.sitio_id = %s ORDER BY e.desde",
            (sitio_id,),
        ):
            print(
                f"{hora(desde)} -> {hora(hasta)}  {motivo:13}  {equipo or 'todo el sitio'}: {desc}"
            )
    return 0


def cmd_clientes_sla(args) -> int:
    with psycopg.connect(_dsn_admin()) as con:
        n = con.execute(
            "UPDATE idcmd.clientes SET meta_disponibilidad = coalesce(%s, meta_disponibilidad),"
            " informe_pdf_adjunto = coalesce(%s, informe_pdf_adjunto)"
            " WHERE id::text = %s OR lower(nombre) = lower(%s)",
            (args.meta, None if args.pdf_adjunto is None else args.pdf_adjunto == "si",
             args.cliente, args.cliente),
        ).rowcount  # fmt: skip
    if n != 1:
        sys.exit(f"'{args.cliente}' coincide con {n} clientes: use el nombre exacto o el id")
    print("SLA del cliente actualizado")
    return 0


def _informe(args, accion) -> int:
    import asyncio
    from datetime import UTC, datetime

    from .informes import informe as inf

    try:
        periodo = inf.periodo_de(args.mes)
    except ValueError:
        sys.exit("--mes debe ser AAAA-MM, p. ej. 2026-10")

    async def principal():
        async with await psycopg.AsyncConnection.connect(_dsn_admin(), autocommit=True) as con:
            return await accion(con, periodo, datetime.now(UTC))

    return asyncio.run(principal())


def cmd_informe_emitir(args) -> int:
    from .informes import informe as inf
    from .informes.emitir import guardar

    async def accion(con, periodo, ahora):
        if not inf.mes_cerrado(periodo, ahora):
            sys.exit("ese mes no ha terminado: el informe se emite al cerrarlo")
        with psycopg.connect(_dsn_admin()) as sinc:
            sitios = [_sitio(sinc, args.sitio)] if args.sitio else sinc.execute(
                "SELECT id, nombre FROM idcmd.sitios ORDER BY nombre").fetchall()  # fmt: skip
        for sitio_id, nombre in sitios:
            async with con.transaction():
                r = await guardar(con, sitio_id, periodo, ahora, recalcular=args.recalcular)
            disp = (
                "—" if r is None or r["disponibilidad"] is None else f"{r['disponibilidad']:.3f} %"
            )
            print(f"{nombre}: {disp}")
        if not args.recalcular:
            print("(los ya emitidos no se tocan; para recalcularlos: --recalcular)")
        return 0

    return _informe(args, accion)


def cmd_informe_pdf(args) -> int:
    from .informes import informe as inf
    from .informes.pdf import a_pdf
    from .portal.informes import _html

    async def accion(con, periodo, ahora):
        with psycopg.connect(_dsn_admin()) as sinc:
            sitio_id, _ = _sitio(sinc, args.sitio)
        r = await inf.construir(con, sitio_id, periodo, ahora)
        args.salida.write_bytes(a_pdf(_html(r)))
        print(f"informe guardado en {args.salida}")
        return 0

    return _informe(args, accion)


def cmd_demo_preparar(args) -> int:
    import asyncio
    from datetime import UTC, datetime

    from . import demo
    from .informes import informe as inf
    from .informes.emitir import guardar

    dsn = _dsn_admin()
    ahora = datetime.now(UTC)

    def crear(correo, nombre, rol):
        clave = generar_clave()
        usuario_id, secreto = crear_usuario(dsn, correo, nombre, rol, clave)
        return usuario_id, (clave, secreto)

    try:
        r, credenciales = demo.preparar(
            dsn, args.correo, args.correo_tecnico, args.salida, args.url_central, crear, ahora
        )
    except ValueError as e:
        sys.exit(str(e))
    print(f"cliente demo listo: sitio '{demo.SITIO}' ({r.sitio_id}), agente {r.agente_id}")
    if r.historial_desde:
        actual = inf.periodo_de(f"{ahora.astimezone(inf.ZONA):%Y-%m}")
        periodos, p = [], r.historial_desde
        while p < actual:
            periodos.append(p)
            p = inf.mes_anterior(p, -1)

        async def emitir():
            async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as con:
                for p in periodos:
                    async with con.transaction():
                        await guardar(con, r.sitio_id, p, ahora, recalcular=True)

        asyncio.run(emitir())
        print(f"historial simulado desde {r.historial_desde:%Y-%m}; informes emitidos: "
              + (", ".join(f"{p:%Y-%m}" for p in periodos) or "ninguno"))  # fmt: skip
    for quien, correo in (("cliente", args.correo), ("tecnico", args.correo_tecnico)):
        cred = credenciales[quien]
        if cred is None:
            print(
                f"\nusuario {quien} {correo}: ya existía"
                " (si hace falta: usuarios restablecer-mfa --generar-clave)"
            )
            continue
        print(f"\n=== Usuario {quien} de la demo ===")
        print(f"  correo: {correo}")
        print(f"  clave del portal: {cred[0]}")
        _mostrar_segundo_factor(correo, cred[1])
    print("\nPara que los equipos simulados funcionen en vivo: sudo idcmd-demo-iniciar")
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
    u_crear.add_argument(
        "--generar-clave", action="store_true", help="clave aleatoria, sin teclear"
    )
    u_crear.set_defaults(fn=cmd_usuarios_crear)
    u_mfa = usuarios.add_parser("restablecer-mfa", help="nuevo segundo factor para un usuario")
    u_mfa.add_argument("--correo", required=True)
    u_mfa.add_argument("--nueva-clave", action="store_true", help="cambiar también la clave")
    u_mfa.add_argument(
        "--generar-clave",
        action="store_true",
        help="cambiar la clave por una aleatoria (sin teclear)",
    )
    u_mfa.set_defaults(fn=cmd_usuarios_restablecer_mfa)
    u_tg = usuarios.add_parser("telegram", help="vincular el chat de Telegram de un usuario")
    u_tg.add_argument("--correo", required=True)
    u_tg.add_argument("--chat-id", type=int, required=True)
    u_tg.set_defaults(fn=cmd_usuarios_telegram)
    sub.add_parser("worker", help="servicio: tickets, escalamiento, SLA y avisos").set_defaults(
        fn=cmd_worker
    )
    sub.add_parser("preparar", help="migraciones y rol de servicio (producción)").set_defaults(
        fn=cmd_preparar
    )
    inventario = sub.add_parser("inventario").add_subparsers(dest="accion", required=True)
    cargar = inventario.add_parser("cargar", help="sitio, equipos y agente desde un YAML")
    cargar.add_argument("--inventario", type=Path, required=True)
    cargar.add_argument("--salida", type=Path, required=True)
    cargar.add_argument("--url-central", required=True, help="p. ej. https://command.idtech.com.co")
    cargar.set_defaults(fn=cmd_inventario_cargar)
    sub.add_parser("sitios", help="sitios con su id").set_defaults(fn=cmd_sitios)
    excl = sub.add_parser("exclusiones", help="tiempo que no cuenta en el SLA").add_subparsers(
        dest="accion", required=True
    )
    e_crear = excl.add_parser("crear")
    e_crear.add_argument("--sitio", required=True, help="nombre o id del sitio")
    e_crear.add_argument("--equipo", help="nombre o id del equipo (sin esto: todo el sitio)")
    e_crear.add_argument("--motivo", choices=["mantenimiento", "causa_cliente"], required=True)
    e_crear.add_argument("--desde", required=True, help="AAAA-MM-DD HH:MM, hora de Colombia")
    e_crear.add_argument("--hasta", required=True, help="AAAA-MM-DD HH:MM, hora de Colombia")
    e_crear.add_argument("--descripcion", required=True)
    e_crear.set_defaults(fn=cmd_exclusiones_crear)
    e_listar = excl.add_parser("listar")
    e_listar.add_argument("--sitio", required=True)
    e_listar.set_defaults(fn=cmd_exclusiones_listar)
    clientes = sub.add_parser("clientes").add_subparsers(dest="accion", required=True)
    c_sla = clientes.add_parser("sla", help="meta de disponibilidad y PDF adjunto del informe")
    c_sla.add_argument("--cliente", required=True)
    c_sla.add_argument("--meta", type=float, help="p. ej. 99.5")
    c_sla.add_argument("--pdf-adjunto", choices=["si", "no"])
    c_sla.set_defaults(fn=cmd_clientes_sla)
    informe = sub.add_parser("informe", help="informe mensual de SLA").add_subparsers(
        dest="accion", required=True
    )
    i_emitir = informe.add_parser("emitir", help="emitir (o recalcular) el informe de un mes")
    i_emitir.add_argument("--mes", required=True, help="AAAA-MM")
    i_emitir.add_argument("--sitio", help="nombre o id (sin esto: todos)")
    i_emitir.add_argument("--recalcular", action="store_true")
    i_emitir.set_defaults(fn=cmd_informe_emitir)
    i_pdf = informe.add_parser("pdf", help="guardar el informe en PDF")
    i_pdf.add_argument("--sitio", required=True)
    i_pdf.add_argument("--mes", required=True)
    i_pdf.add_argument("--salida", type=Path, required=True)
    i_pdf.set_defaults(fn=cmd_informe_pdf)
    demo_p = sub.add_parser("demo", help="cliente de demostración").add_subparsers(
        dest="accion", required=True
    )
    d_prep = demo_p.add_parser("preparar", help="crear o completar el cliente demo")
    d_prep.add_argument("--correo", required=True, help="usuario cliente de la demo")
    d_prep.add_argument("--correo-tecnico", required=True, help="técnico de la demo")
    d_prep.add_argument("--salida", type=Path, default=Path("/sitios/demo"))
    d_prep.add_argument("--url-central", default="http://localhost:8000")
    d_prep.set_defaults(fn=cmd_demo_preparar)
    lab = sub.add_parser("laboratorio").add_subparsers(dest="accion", required=True)
    preparar = lab.add_parser("preparar")
    preparar.add_argument("--inventario", type=Path, required=True)
    preparar.add_argument("--salida", type=Path, required=True)
    preparar.add_argument("--url-central", default="http://localhost:8000")
    preparar.set_defaults(fn=cmd_laboratorio_preparar)
    args = p.parse_args(argv)
    return args.fn(args)
