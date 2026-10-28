"""Inventario de un sitio desde un archivo YAML: lo carga en la central y genera la
configuración del agente con los MISMOS identificadores.

Pensado para el laboratorio y los primeros pilotos (en la Etapa 2 el agente descargará su
inventario de la central). Los identificadores de los equipos son deterministas
(uuid5 del sitio + clave del equipo): correr la carga dos veces no duplica nada.

    cliente: IDtech
    regional: Barranquilla
    sitio: Oficina IDtech
    ciudad: Barranquilla
    direccion: Calle 00 # 00-00          # opcional: "Cómo llegar" para el técnico
    lat: 10.9878                         # opcional: confirma la llegada del técnico
    lon: -74.7889
    dispositivos:
      - clave: nvr
        nombre: NVR oficina
        tipo_equipo: nvr
        marca: dahua
        modelo: DHI-NVR5432-EI
        direccion: 192.168.1.16
        credencial_ref: nvr
        retencion_contratada_dias: 30
        canales: {1: camara_1, 2: camara_2}     # canal -> clave de la cámara
        chequeos:
          - {adaptador: dahua_nvr, con_canales: true}
          - {adaptador: dahua_retencion, intervalo_s: 900, deteccion: false, con_canales: true,
             parametros: {retencion_contratada_dias: 30}}
      - clave: camara_1
        nombre: Cámara entrada
        tipo_equipo: camara
        marca: dahua

Opcional, para el módulo de servicio:

    tecnicos:                 # usuarios del portal con rol tecnico (ya creados)
      - {correo: tecnico@idtech.co, tipo: principal}
    contactos:                # personas del cliente que siguen los tickets del sitio
      - {nombre: Jefe de seguridad, correo: seguridad@cliente.co}

El archivo NUNCA lleva claves: `credencial_ref` es el nombre con que la clave se guardó en
la bóveda del agente (`idcmd credenciales guardar`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import psycopg
import yaml

_NS = uuid.UUID("2f1d6b8e-6a52-4f7e-9d0c-8f3b5a1c7e44")
TIPOS = {"camara", "nvr", "dvr", "servidor_vms", "lectora", "controladora", "switch", "ups", "otro"}


@dataclass(frozen=True)
class Resultado:
    cliente_id: uuid.UUID
    regional_id: uuid.UUID
    sitio_id: uuid.UUID
    dispositivos: dict[str, uuid.UUID]


def leer(archivo: Path) -> dict:
    datos = yaml.safe_load(archivo.read_text(encoding="utf-8"))
    for campo in ("cliente", "regional", "sitio", "dispositivos"):
        if not datos.get(campo):
            raise ValueError(f"falta '{campo}' en {archivo}")
    claves = [d["clave"] for d in datos["dispositivos"]]
    if len(claves) != len(set(claves)):
        raise ValueError("hay claves de equipo repetidas")
    for d in datos["dispositivos"]:
        if d.get("tipo_equipo") not in TIPOS:
            raise ValueError(f"{d['clave']}: tipo_equipo debe ser uno de {sorted(TIPOS)}")
        for canal, camara in (d.get("canales") or {}).items():
            if camara not in claves:
                raise ValueError(
                    f"{d['clave']}: el canal {canal} apunta a '{camara}', que no existe"
                )
    return datos


def id_dispositivo(sitio_id: uuid.UUID, clave: str) -> uuid.UUID:
    return uuid.uuid5(_NS, f"{sitio_id}/{clave}")


def cargar(dsn_dueno: str, datos: dict) -> Resultado:
    """Crea (o reutiliza por nombre) cliente, regional y sitio, y crea o actualiza equipos."""
    with psycopg.connect(dsn_dueno) as con, con.transaction():

        def obtener(sql_buscar, sql_crear, params_buscar, params_crear):
            fila = con.execute(sql_buscar, params_buscar).fetchone()
            return fila[0] if fila else con.execute(sql_crear, params_crear).fetchone()[0]

        cliente = obtener(
            "SELECT id FROM idcmd.clientes WHERE nombre = %s",
            "INSERT INTO idcmd.clientes (nombre) VALUES (%s) RETURNING id",
            (datos["cliente"],),
            (datos["cliente"],),
        )
        regional = obtener(
            "SELECT id FROM idcmd.regionales WHERE cliente_id = %s AND nombre = %s",
            "INSERT INTO idcmd.regionales (cliente_id, nombre) VALUES (%s, %s) RETURNING id",
            (cliente, datos["regional"]),
            (cliente, datos["regional"]),
        )
        sitio = obtener(
            "SELECT id FROM idcmd.sitios WHERE cliente_id = %s AND nombre = %s",
            "INSERT INTO idcmd.sitios (cliente_id, regional_id, nombre, ciudad)"
            " VALUES (%s, %s, %s, %s) RETURNING id",
            (cliente, datos["sitio"]),
            (cliente, regional, datos["sitio"], datos.get("ciudad")),
        )
        # Dirección y coordenadas: "Cómo llegar" del técnico y confirmación de la llegada.
        con.execute(
            "UPDATE idcmd.sitios SET ciudad = coalesce(%s, ciudad),"
            " direccion = coalesce(%s, direccion), lat = coalesce(%s, lat),"
            " lon = coalesce(%s, lon) WHERE id = %s",
            (
                datos.get("ciudad"),
                datos.get("direccion"),
                datos.get("lat"),
                datos.get("lon"),
                sitio,
            ),
        )
        ids = {}
        for d in datos["dispositivos"]:
            ids[d["clave"]] = id_dispositivo(sitio, d["clave"])
            con.execute(
                """
                INSERT INTO idcmd.dispositivos (id, cliente_id, sitio_id, nombre, tipo_equipo,
                    marca, modelo, direccion, ubicacion, retencion_contratada_dias,
                    credencial_ref, criticidad)
                VALUES (%(id)s, %(cliente)s, %(sitio)s, %(nombre)s, %(tipo)s, %(marca)s,
                        %(modelo)s, %(direccion)s, %(ubicacion)s, %(retencion)s, %(cred)s,
                        %(criticidad)s)
                ON CONFLICT (id) DO UPDATE SET
                    nombre = EXCLUDED.nombre, tipo_equipo = EXCLUDED.tipo_equipo,
                    marca = EXCLUDED.marca, modelo = EXCLUDED.modelo,
                    direccion = EXCLUDED.direccion, ubicacion = EXCLUDED.ubicacion,
                    retencion_contratada_dias = EXCLUDED.retencion_contratada_dias,
                    credencial_ref = EXCLUDED.credencial_ref,
                    criticidad = EXCLUDED.criticidad, activo = true
                """,
                {
                    "id": ids[d["clave"]],
                    "cliente": cliente,
                    "sitio": sitio,
                    "nombre": d.get("nombre", d["clave"]),
                    "tipo": d["tipo_equipo"],
                    "marca": d.get("marca"),
                    "modelo": d.get("modelo"),
                    "direccion": d.get("direccion"),
                    "ubicacion": d.get("ubicacion"),
                    "retencion": d.get("retencion_contratada_dias"),
                    "cred": d.get("credencial_ref"),
                    "criticidad": d.get("criticidad", 2),
                },
            )
        avisos = _cargar_servicio(con, datos, cliente, sitio)
    for aviso in avisos:
        print(f"aviso: {aviso}")
    return Resultado(cliente, regional, sitio, ids)


def _cargar_servicio(con, datos: dict, cliente, sitio) -> list[str]:
    """Técnicos de la zona y contactos del cliente. Idempotente."""
    avisos = []
    for t in datos.get("tecnicos") or []:
        tipo = t.get("tipo", "principal")
        fila = con.execute(
            "SELECT id, rol FROM idcmd.usuarios WHERE correo = %s", (t["correo"],)
        ).fetchone()
        if not fila or fila[1] != "tecnico":
            avisos.append(
                f"técnico {t['correo']} no existe o no tiene rol tecnico: créelo con"
                " 'idcmd-central usuarios crear --rol tecnico' y repita"
            )
            continue
        vigente = con.execute(
            "SELECT tecnico_id FROM idcmd.asignaciones_tecnico WHERE sitio_id = %s AND tipo = %s"
            " AND vigente_hasta IS NULL",
            (sitio, tipo),
        ).fetchone()
        if vigente and vigente[0] == fila[0]:
            continue
        if vigente:
            con.execute(
                "UPDATE idcmd.asignaciones_tecnico SET vigente_hasta = now() WHERE sitio_id = %s"
                " AND tipo = %s AND vigente_hasta IS NULL",
                (sitio, tipo),
            )
        con.execute(
            "INSERT INTO idcmd.asignaciones_tecnico (tecnico_id, cliente_id, sitio_id, tipo)"
            " VALUES (%s, %s, %s, %s)",
            (fila[0], cliente, sitio, tipo),
        )
    for c in datos.get("contactos") or []:
        con.execute(
            "INSERT INTO idcmd.contactos_sitio (cliente_id, sitio_id, nombre, correo,"
            " telegram_chat_id) VALUES (%s, %s, %s, %s, %s)"
            " ON CONFLICT (sitio_id, correo) DO UPDATE SET nombre = excluded.nombre,"
            " telegram_chat_id = excluded.telegram_chat_id, activo = true",
            (cliente, sitio, c["nombre"], c.get("correo"), c.get("telegram_chat_id")),
        )
    return avisos


def config_agente(
    datos: dict, resultado: Resultado, agente_id: uuid.UUID, url_central: str
) -> dict:
    """Configuración del agente (mismo formato que agente/config.ejemplo.yaml)."""
    ids = resultado.dispositivos
    objetivos = []
    for d in datos["dispositivos"]:
        for ch in d.get("chequeos") or []:
            objetivo = {
                "dispositivo_id": str(ids[d["clave"]]),
                "adaptador": ch["adaptador"],
                "direccion": d["direccion"],
                "intervalo_s": ch.get("intervalo_s", 60),
            }
            if d.get("credencial_ref") and ch["adaptador"] != "icmp":
                objetivo["credencial_ref"] = d["credencial_ref"]
            if ch.get("deteccion") is False:
                objetivo["deteccion"] = False
            if ch.get("parametros"):
                objetivo["parametros"] = ch["parametros"]
            if ch.get("con_canales") and d.get("canales"):
                objetivo["canales"] = {
                    int(canal): str(ids[camara]) for canal, camara in d["canales"].items()
                }
            objetivos.append(objetivo)
    return {
        "agente_id": str(agente_id),
        # El token y esta configuración se montan juntos en /config (solo lectura); la
        # bóveda con las claves de los equipos, aparte en /secretos.
        "central": {"url": url_central, "archivo_token": "/config/token_agente"},
        "almacenamiento": {
            "buzon": "/datos/buzon.sqlite3",
            "boveda": "/secretos/boveda.json",
            "archivo_llave": "/secretos/llave",
        },
        "objetivos": objetivos,
    }
