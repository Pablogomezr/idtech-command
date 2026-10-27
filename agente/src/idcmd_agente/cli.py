"""Consola local del agente (`idcmd`). Las credenciales se cargan SOLO por aquí.

idcmd ejecutar      --config /config/agente.yaml
idcmd llave generar --salida /secretos/idcmd_llave
idcmd credenciales guardar <ref> --usuario <usuario>   (pide la clave sin eco)
idcmd credenciales listar
idcmd credenciales eliminar <ref>
idcmd diagnostico
idcmd buzon reenviar --desde 2026-10-01T00:00:00-05:00
idcmd credenciales guardar nvr --usuario idcmd_monitor --secretos /secretos   (laboratorio)
idcmd sondear --adaptador hikvision_acceso --direccion 192.168.1.80 --usuario admin \
              [--param lectoras_esperadas=2] [--canales 1-8] [--capturar ./captura]
              (o --credencial nvr --secretos /secretos en vez de --usuario)
    Prueba un equipo una vez y muestra lo que el agente vería. No guarda la clave, no
    necesita configuración ni central. Para validar adaptadores con equipos reales.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

from pydantic import SecretStr

from .adaptadores import descubrir, obtener
from .boveda import Boveda, BovedaError, Credencial
from .config import cargar_config
from .nucleo.buzon import Buzon
from .nucleo.modelos import Objetivo

CONFIG_POR_DEFECTO = Path("/config/agente.yaml")


def _boveda(args, crear: bool = False) -> Boveda:
    """Bóveda del agente (según la configuración) o, con --secretos, una carpeta local de
    laboratorio con `boveda.json` y `llave` (la llave se crea en el primer `guardar`)."""
    if getattr(args, "secretos", None):
        carpeta = Path(args.secretos)
        llave = carpeta / "llave"
        if crear and not llave.exists():
            carpeta.mkdir(parents=True, exist_ok=True)
            Boveda.escribir_llave(llave, Boveda.generar_llave())
        return Boveda.desde_archivos(carpeta / "boveda.json", llave)
    alm = cargar_config(args.config).almacenamiento
    return Boveda.desde_archivos(alm.boveda, alm.archivo_llave)


def cmd_ejecutar(args) -> int:
    from .agente import ejecutar_agente

    asyncio.run(ejecutar_agente(cargar_config(args.config)))
    return 0


def cmd_llave_generar(args) -> int:
    Boveda.escribir_llave(args.salida, Boveda.generar_llave())
    print(f"llave creada en {args.salida} (permisos 0600). Guárdela como Docker secret.")
    return 0


def cmd_credenciales_guardar(args) -> int:
    clave = getpass.getpass("Clave: ")
    if clave != getpass.getpass("Repita la clave: "):
        print("las claves no coinciden", file=sys.stderr)
        return 1
    _boveda(args, crear=True).guardar(
        args.ref, Credencial(usuario=args.usuario, clave=SecretStr(clave))
    )
    print(f"credencial {args.ref!r} guardada cifrada en este equipo")
    return 0


def cmd_credenciales_listar(args) -> int:
    for ref in _boveda(args).listar():
        print(ref)
    return 0


def cmd_credenciales_eliminar(args) -> int:
    _boveda(args).eliminar(args.ref)
    print(f"credencial {args.ref!r} eliminada")
    return 0


def cmd_diagnostico(args) -> int:
    config = cargar_config(args.config)
    adaptadores = descubrir()
    boveda = Boveda.desde_archivos(
        config.almacenamiento.boveda, config.almacenamiento.archivo_llave
    )
    refs = set(boveda.listar())
    print(f"agente:        {config.agente_id}")
    print(f"central:       {config.central.url}")
    print(f"adaptadores:   {', '.join(adaptadores)}")
    por_adaptador = Counter(o.adaptador for o in config.objetivos)
    print(f"objetivos:     {dict(por_adaptador)}")
    sin_adaptador = sorted(set(por_adaptador) - set(adaptadores))
    faltantes = sorted({o.credencial_ref for o in config.objetivos if o.credencial_ref} - refs)
    if config.almacenamiento.buzon.exists():
        buzon = Buzon(config.almacenamiento.buzon)
        print(f"pendientes:    {buzon.pendientes()}")
        buzon.cerrar()
    if sin_adaptador:
        print(f"ERROR adaptadores no registrados: {', '.join(sin_adaptador)}")
    if faltantes:
        print(f"ERROR credenciales faltantes en la bóveda: {', '.join(faltantes)}")
    return 1 if (sin_adaptador or faltantes) else 0


def cmd_buzon_reenviar(args) -> int:
    buzon = Buzon(cargar_config(args.config).almacenamiento.buzon)
    n = buzon.reenviar_desde(datetime.fromisoformat(args.desde))
    buzon.cerrar()
    print(f"{n} ítems marcados para reenvío")
    return 0


def _valor(texto: str) -> str | int | float | bool:
    if texto.lower() in ("true", "false"):
        return texto.lower() == "true"
    for tipo in (int, float):
        try:
            return tipo(texto)
        except ValueError:
            pass
    return texto


def _rango_canales(texto: str | None) -> list[int]:
    """'1-4,7' -> [1, 2, 3, 4, 7]."""
    canales: list[int] = []
    for parte in filter(None, (texto or "").split(",")):
        inicio, _, fin = parte.partition("-")
        canales += range(int(inicio), int(fin or inicio) + 1)
    return canales


def cmd_sondear(args) -> int:
    descubrir()
    clase = obtener(args.adaptador)
    credencial = None
    if args.credencial:
        credencial = _boveda(args).obtener(args.credencial)
    elif args.usuario:
        credencial = Credencial(usuario=args.usuario, clave=SecretStr(getpass.getpass("Clave: ")))
    parametros = dict(par.split("=", 1) for par in args.param)
    canales = {n: uuid.uuid4() for n in _rango_canales(args.canales)}
    objetivo = Objetivo(
        dispositivo_id=uuid.uuid4(),
        adaptador=args.adaptador,
        direccion=args.direccion,
        parametros={k: _valor(v) for k, v in parametros.items()},
        canales=canales,
    )
    etiquetas = {objetivo.dispositivo_id: "equipo", **{d: f"canal {n}" for n, d in canales.items()}}
    if args.capturar:
        # Validar antes de consultar el equipo: un error aquí no debe verse como falla del equipo.
        try:
            args.capturar.mkdir(parents=True, exist_ok=True)
            (args.capturar / ".prueba").write_bytes(b"")
            (args.capturar / ".prueba").unlink()
        except OSError:
            print(
                f"no se puede escribir en {args.capturar}. En Docker, monte la carpeta con "
                "-v ${PWD}/captura:/captura y use --capturar /captura (misma ruta).",
                file=sys.stderr,
            )
            return 2
    try:
        adaptador = clase(capturar_en=args.capturar) if args.capturar else clase()
    except TypeError:
        print(f"{args.adaptador} no admite --capturar", file=sys.stderr)
        return 2
    resultados = asyncio.run(adaptador.chequear(objetivo, credencial))
    for r in resultados:
        datos = {"de": etiquetas.get(r.dispositivo_id, "?")}
        datos |= r.model_dump(mode="json", exclude={"dispositivo_id"})
        print(json.dumps(datos, indent=2, ensure_ascii=False))
    if args.capturar:
        print(f"respuestas crudas guardadas en {args.capturar} (revíselas antes de compartirlas)")
    return 0


def construir_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="idcmd", description="Agente en sitio de IDtech Command")
    p.add_argument("--config", type=Path, default=CONFIG_POR_DEFECTO)
    sub = p.add_subparsers(dest="comando", required=True)

    sub.add_parser("ejecutar", help="inicia el agente").set_defaults(fn=cmd_ejecutar)

    llave = sub.add_parser("llave").add_subparsers(dest="accion", required=True)
    generar = llave.add_parser("generar", help="crea la llave de la bóveda")
    generar.add_argument("--salida", type=Path, required=True)
    generar.set_defaults(fn=cmd_llave_generar)

    cred = sub.add_parser("credenciales").add_subparsers(dest="accion", required=True)
    ayuda_secretos = "carpeta local de laboratorio (boveda.json + llave), en vez de --config"
    guardar = cred.add_parser("guardar", help="guarda o reemplaza una credencial")
    guardar.add_argument("ref")
    guardar.add_argument("--usuario", required=True)
    guardar.add_argument("--secretos", type=Path, help=ayuda_secretos)
    guardar.set_defaults(fn=cmd_credenciales_guardar)
    listar = cred.add_parser("listar")
    listar.add_argument("--secretos", type=Path, help=ayuda_secretos)
    listar.set_defaults(fn=cmd_credenciales_listar)
    eliminar = cred.add_parser("eliminar")
    eliminar.add_argument("ref")
    eliminar.add_argument("--secretos", type=Path, help=ayuda_secretos)
    eliminar.set_defaults(fn=cmd_credenciales_eliminar)

    sub.add_parser("diagnostico", help="verifica la instalación").set_defaults(fn=cmd_diagnostico)

    sondear = sub.add_parser("sondear", help="prueba un equipo una sola vez")
    sondear.add_argument("--adaptador", required=True)
    sondear.add_argument("--direccion", required=True)
    quien = sondear.add_mutually_exclusive_group()
    quien.add_argument("--usuario", help="pide la clave sin eco; no se guarda")
    quien.add_argument("--credencial", metavar="REF", help="usa una credencial de la bóveda")
    sondear.add_argument("--secretos", type=Path, help=ayuda_secretos)
    sondear.add_argument("--param", action="append", default=[], metavar="CLAVE=VALOR")
    sondear.add_argument("--canales", metavar="1-8", help="canales del NVR a revisar")
    sondear.add_argument("--capturar", type=Path, metavar="DIRECTORIO")
    sondear.set_defaults(fn=cmd_sondear)

    buzon = sub.add_parser("buzon").add_subparsers(dest="accion", required=True)
    reenviar = buzon.add_parser("reenviar", help="reenvía lo ya enviado desde una fecha")
    reenviar.add_argument("--desde", required=True)
    reenviar.set_defaults(fn=cmd_buzon_reenviar)
    return p


def main(argv: list[str] | None = None) -> int:
    args = construir_parser().parse_args(argv)
    try:
        return args.fn(args)
    except BovedaError as e:
        print(f"error de bóveda: {e}", file=sys.stderr)
        return 2
    except KeyError as e:
        print(f"no existe: {e}", file=sys.stderr)
        return 2
