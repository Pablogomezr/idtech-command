"""Cliente de demostración para mostrar IDtech Command a prospectos sin tocar datos reales.

Crea (y si ya existe, reutiliza) "Cliente Demo" con una sede de equipos SIMULADOS (adaptador
`simulado`), un técnico y un usuario cliente, la configuración de un agente simulado que corre en
el mismo servidor, y un historial simulado de los últimos meses (caídas, servicios cerrados e
informes mensuales) para que el portal no arranque vacío. Todo queda marcado como demo: nunca
genera avisos (migración 0009) y los nombres lo dicen.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import psycopg
import yaml

from .informes.calculo import truncar_hora
from .portal.presentacion import ZONA
from .servicio.reglas import prioridad
from .servicio.worker import SLA_POR_DEFECTO

ESPACIO = uuid.UUID("5b0d6f3e-3c1a-4f9a-9e55-2d6c1a7d0e01")  # ids estables de la demo
CLIENTE = "Cliente Demo (datos simulados)"
SITIO = "Sede Demo"
# (clave, nombre, tipo, criticidad, caídas al mes, retención contratada)
EQUIPOS = [
    ("nvr", "NVR principal", "nvr", 1, 0.6, 30),
    ("cam1", "Cámara 1 · entrada principal", "camara", 2, 1.2, 30),
    ("cam2", "Cámara 2 · recepción", "camara", 2, 0.8, 30),
    ("cam3", "Cámara 3 · parqueadero", "camara", 2, 3.0, 30),
    ("cam4", "Cámara 4 · bodega", "camara", 3, 1.0, 30),
    ("cam5", "Cámara 5 · cerramiento norte", "camara", 2, 2.0, 30),
    ("panel", "Panel de acceso · recepción", "controladora", 1, 0.8, None),
]
# Referencias comerciales comunes (no de un cliente): el tablero dibuja cada equipo según ellas.
MODELOS = {
    "nvr": ("dahua", "DHI-NVR5432-EI"),
    "cam1": ("hikvision", "DS-2CD2T47G2-L"),
    "cam2": ("hikvision", "DS-2CD2147G2-SU"),
    "cam3": ("hikvision", "DS-2DE4425IW-DE"),
    "cam4": ("dahua", "IPC-HDW2441T-S"),
    "cam5": ("dahua", "IPC-HFW2441S-S"),
    "panel": ("hikvision", "DS-K2602T"),
}


def _id(clave: str) -> uuid.UUID:
    return uuid.uuid5(ESPACIO, clave)


@dataclass
class Resultado:
    sitio_id: uuid.UUID
    agente_id: uuid.UUID
    token: str | None  # solo la primera vez
    historial_desde: date | None  # None: ya tenía historial, no se tocó


def _crear_estructura(con: psycopg.Connection) -> None:
    cliente, regional, sitio = _id("cliente"), _id("regional"), _id("sitio")
    con.execute(
        "INSERT INTO idcmd.clientes (id, nombre, es_demo, meta_disponibilidad) VALUES"
        " (%s, %s, true, 99.5) ON CONFLICT (id) DO UPDATE SET es_demo = true",
        (cliente, CLIENTE),
    )
    con.execute(
        "INSERT INTO idcmd.regionales (id, cliente_id, nombre) VALUES (%s, %s, 'Demostración')"
        " ON CONFLICT (id) DO NOTHING",
        (regional, cliente),
    )
    con.execute(
        "INSERT INTO idcmd.sitios (id, cliente_id, regional_id, nombre, ciudad, direccion, lat,"
        " lon) VALUES (%s, %s, %s, %s, 'Barranquilla', 'Dirección de demostración', 10.9878,"
        " -74.7889) ON CONFLICT (id) DO NOTHING",
        (sitio, cliente, regional, SITIO),
    )
    for clave, nombre, tipo, criticidad, _, retencion in EQUIPOS:
        con.execute(
            "INSERT INTO idcmd.dispositivos (id, cliente_id, sitio_id, nombre, tipo_equipo, marca,"
            " modelo, direccion, criticidad, retencion_contratada_dias) VALUES"
            " (%s, %s, %s, %s, %s, %s, %s, 'demo', %s, %s)"
            # Las demos ya creadas toman las referencias al volver a prepararlas.
            " ON CONFLICT (id) DO UPDATE SET marca = EXCLUDED.marca, modelo = EXCLUDED.modelo",
            (_id(clave), cliente, sitio, nombre, tipo, *MODELOS[clave], criticidad, retencion),
        )


def _usuario(con, correo: str, nombre: str, rol: str, crear) -> tuple[uuid.UUID, tuple | None]:
    """Crea el usuario si no existe. Devuelve (id, (clave, secreto)) o (id, None) si ya existía."""
    fila = con.execute("SELECT id, rol FROM idcmd.usuarios WHERE correo = %s", (correo,)).fetchone()
    if fila:
        if fila[1] != rol:
            raise ValueError(f"{correo} ya existe con rol {fila[1]}, no {rol}")
        return fila[0], None
    return crear(correo, nombre, rol)


def _agente(dsn: str, salida: Path, url_central: str) -> tuple[uuid.UUID, str | None]:
    from .cli import crear_agente  # evita importación circular con la consola

    archivo_config = salida / "agente" / "agente.yaml"
    archivo_token = salida / "agente" / "token_agente"
    agente_id, token = None, None
    if archivo_config.exists() and archivo_token.exists():
        agente_id = uuid.UUID(yaml.safe_load(archivo_config.read_text())["agente_id"])
    if agente_id is None:
        agente_id, token = crear_agente(dsn, _id("sitio"), "Agente demo")
    (salida / "agente").mkdir(parents=True, exist_ok=True)
    (salida / "secretos").mkdir(parents=True, exist_ok=True)
    if token:
        archivo_token.write_text(token + "\n")
    llave = salida / "secretos" / "llave"
    if not llave.exists():  # la bóveda queda vacía: los equipos simulados no tienen clave
        import base64
        import os

        llave.write_text(base64.b64encode(os.urandom(32)).decode() + "\n")
    objetivos = [
        {
            "dispositivo_id": str(_id(clave)),
            "adaptador": "simulado",
            "direccion": "demo",
            "intervalo_s": 60,
            # ~1 caída cada 3-4 días por equipo, de 5 a 25 min: se ven tickets abrirse y cerrarse.
            "parametros": {"prob_caida": 0.0002, "duracion_caida_s": 900, "semilla": 7}
            # Retención solo en el parqueadero: una novedad visible, no media sede en amarillo.
            | ({"retencion_contratada_dias": retencion} if clave == "cam3" else {}),
        }
        for clave, _, _, _, _, retencion in EQUIPOS
    ]
    config = {
        "agente_id": str(agente_id),
        "central": {"url": url_central, "archivo_token": "/config/token_agente"},
        "almacenamiento": {
            "buzon": "/datos/buzon.sqlite3",
            "boveda": "/secretos/boveda.json",
            "archivo_llave": "/secretos/llave",
        },
        "objetivos": objetivos,
    }
    archivo_config.write_text(
        "# Agente de DEMOSTRACIÓN (equipos simulados).\n"
        "# Generado por 'idcmd-central demo preparar'.\n"
        + yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
    )
    return agente_id, token


def _historial(con, agente_id: uuid.UUID, tecnico_id: uuid.UUID, ahora: datetime) -> date:
    """Dos meses completos y el actual: caídas, horas con datos, servicios cerrados."""
    cliente, sitio = _id("cliente"), _id("sitio")
    hoy = ahora.astimezone(ZONA)
    inicio_mes = date(hoy.year, hoy.month, 1)
    total = inicio_mes.year * 12 + inicio_mes.month - 1 - 2
    desde_fecha = date(total // 12, total % 12 + 1, 1)
    desde = datetime(desde_fecha.year, desde_fecha.month, 1, tzinfo=ZONA)
    hasta = truncar_hora(ahora) - timedelta(hours=1)
    azar = random.Random(20261031)

    # Una noche sin internet en el sitio: el informe lo muestra como "sin datos", no como caída.
    apagon = desde + timedelta(days=17, hours=1)
    sin_datos = (apagon, apagon + timedelta(hours=5))

    for clave, nombre, tipo, criticidad, por_mes, _ in EQUIPOS:
        disp = _id(clave)

        def evento(momento, anterior, nuevo, codigo=None, disp=disp):
            con.execute(
                "INSERT INTO idcmd.eventos_estado (id, dispositivo_id, cliente_id, sitio_id,"
                " agente_id, chequeo, estado_anterior, estado_nuevo, observado_at, recibido_at,"
                " codigo) VALUES (%s, %s, %s, %s, %s, 'simulado', %s, %s, %s, %s, %s)",
                (uuid.uuid4(), disp, cliente, sitio, agente_id, anterior, nuevo, momento,
                 momento, codigo),
            )  # fmt: skip

        evento(desde, "desconocido", "ok")
        con.execute(
            "INSERT INTO idcmd.agregados_hora (id, dispositivo_id, cliente_id, sitio_id,"
            " agente_id, chequeo, hora, muestras_ok, muestras_degradado, muestras_caido,"
            " muestras_desconocido) SELECT gen_random_uuid(), %s, %s, %s, %s, 'simulado', h, 60,"
            " 0, 0, 0 FROM generate_series(%s::timestamptz, %s::timestamptz, interval '1 hour') h"
            " WHERE NOT (h >= %s AND h < %s)",
            (disp, cliente, sitio, agente_id, desde, hasta, *sin_datos),
        )
        dias = (hasta - desde).total_seconds() / 86400
        inicios = sorted(
            desde + timedelta(seconds=azar.uniform(0, (hasta - desde).total_seconds()))
            for _ in range(max(1, round(por_mes * dias / 30)))
        )
        libre_desde = desde
        for momento in inicios:
            if (
                momento < libre_desde
                or sin_datos[0] - timedelta(hours=6) <= momento <= sin_datos[1]
            ):
                continue  # sin caídas encimadas ni pegadas al apagón
            duracion = timedelta(minutes=azar.choice([6, 12, 25, 45, 90, 150, 240]))
            fin = min(momento + duracion, hasta - timedelta(minutes=5))
            if fin <= momento:
                continue
            evento(momento, "ok", "caido", "caida_simulada")
            evento(fin, "caido", "ok")
            _ticket(con, azar, disp, nombre, tipo, criticidad, tecnico_id, momento, fin)
            libre_desde = fin + timedelta(hours=1)
    return desde_fecha


def _ticket(con, azar, disp, nombre, tipo, criticidad, tecnico_id, inicio, fin) -> None:
    cliente, sitio = _id("cliente"), _id("sitio")
    prio, tipo_ticket = prioridad("caida_simulada", tipo, criticidad)
    sla = SLA_POR_DEFECTO[prio]
    corta = fin - inicio < timedelta(minutes=30)
    asignado = inicio + timedelta(minutes=1)
    aceptado = None if corta else inicio + timedelta(minutes=azar.choice([8, 15, 25, 40, 70]))
    if aceptado and azar.random() < 0.08:  # algunos fuera de plazo: el reporte debe mostrarlos
        aceptado = inicio + timedelta(minutes=sla["respuesta_min"] + azar.choice([20, 45, 90]))
    en_sitio = None if corta else aceptado + timedelta(minutes=azar.choice([35, 50, 80]))
    resuelto = None if corta else max(fin, en_sitio + timedelta(minutes=10))
    cerrado = fin + timedelta(minutes=10) if corta else resuelto + timedelta(minutes=10)
    ticket_id = uuid.uuid4()
    con.execute(
        "INSERT INTO idcmd.tickets (id, cliente_id, sitio_id, dispositivo_id, origen, tipo,"
        " prioridad, codigo, titulo, estado, tecnico_id, abierto_at, asignado_at, aceptado_at,"
        " en_sitio_at, resuelto_at, cerrado_at, vence_respuesta_at, vence_solucion_at, cierre)"
        " VALUES (%s, %s, %s, %s, 'automatico', %s, %s, 'caida_simulada', 'Caída (simulada)',"
        " 'cerrado', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (ticket_id, cliente, sitio, disp, tipo_ticket, prio, tecnico_id, inicio, asignado,
         aceptado, en_sitio, resuelto, cerrado, inicio + timedelta(minutes=sla["respuesta_min"]),
         inicio + timedelta(minutes=sla["solucion_min"]),
         "recuperado_solo" if corta else "verificado"),
    )  # fmt: skip
    linea = [(inicio, "apertura", f"Falla detectada — {nombre}: caída (simulada)"),
             (asignado, "asignacion", "Asignado al técnico de la zona: Técnico Demo")]  # fmt: skip
    if corta:
        linea.append((cerrado, "recuperado_solo", "El equipo se recuperó solo y se verificó"))
    else:
        linea += [(aceptado, "aceptar", "El técnico aceptó el servicio"),
                  (en_sitio, "en_sitio", "El técnico llegó al sitio"),
                  (resuelto, "resuelto", "Resuelto por el técnico"),
                  (cerrado, "cierre", "Equipo verificado en línea: servicio cerrado")]  # fmt: skip
    for momento, tipo_evento, texto in linea:
        con.execute(
            "INSERT INTO idcmd.eventos_ticket (ticket_id, cliente_id, sitio_id, at, tipo, texto,"
            " usuario_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (ticket_id, cliente, sitio, momento, tipo_evento, texto,
             tecnico_id if tipo_evento in ("aceptar", "en_sitio", "resuelto") else None),
        )  # fmt: skip


def preparar(
    dsn: str, correo_cliente: str, correo_tecnico: str, salida: Path, url_central: str,
    crear_usuario, ahora: datetime,
) -> tuple[Resultado, dict[str, tuple | None]]:  # fmt: skip
    """`crear_usuario(correo, nombre, rol) -> (id, (clave, secreto))` lo pone la consola."""
    with psycopg.connect(dsn) as con:
        _crear_estructura(con)
        con.commit()
        tecnico_id, cred_tecnico = _usuario(con, correo_tecnico, "Técnico Demo", "tecnico",
                                            crear_usuario)  # fmt: skip
        cliente_id, cred_cliente = _usuario(con, correo_cliente, "Usuario Demo (cliente)",
                                            "cliente", crear_usuario)  # fmt: skip
        con.execute(
            "INSERT INTO idcmd.alcances_usuario (usuario_id, cliente_id) SELECT %s, %s"
            " WHERE NOT EXISTS (SELECT 1 FROM idcmd.alcances_usuario WHERE usuario_id = %s)",
            (cliente_id, _id("cliente"), cliente_id),
        )
        con.execute(
            "INSERT INTO idcmd.asignaciones_tecnico"
            " (tecnico_id, cliente_id, sitio_id, vigente_desde)"
            " SELECT %s, %s, %s, %s WHERE NOT EXISTS (SELECT 1 FROM idcmd.asignaciones_tecnico"
            " WHERE sitio_id = %s AND vigente_hasta IS NULL)",
            (tecnico_id, _id("cliente"), _id("sitio"), ahora - timedelta(days=120), _id("sitio")),
        )
        con.commit()
        agente_id, token = _agente(dsn, salida, url_central)
        tiene = con.execute(
            "SELECT 1 FROM idcmd.eventos_estado WHERE sitio_id = %s LIMIT 1", (_id("sitio"),)
        ).fetchone()
        historial = None
        if not tiene:
            historial = _historial(con, agente_id, tecnico_id, ahora)
            con.execute(
                "INSERT INTO idcmd.exclusiones_sla (cliente_id, sitio_id, dispositivo_id, motivo,"
                " desde, hasta, descripcion) VALUES (%s, %s, %s, 'mantenimiento', %s, %s,"
                " 'Mantenimiento preventivo acordado (demostración)')",
                (_id("cliente"), _id("sitio"), _id("panel"),
                 ahora - timedelta(days=40, hours=3), ahora - timedelta(days=40)),
            )  # fmt: skip
        con.commit()
    return (
        Resultado(_id("sitio"), agente_id, token, historial),
        {"cliente": cred_cliente, "tecnico": cred_tecnico},
    )
