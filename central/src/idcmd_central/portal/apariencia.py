"""Apariencia de cada equipo en el tablero: la figura sale de la referencia del modelo.

Las figuras son ilustraciones propias (SVG en `_figuras.html`), no fotos de los fabricantes: no
dependen de la nube de nadie ni de derechos de imagen. Si el administrador sube una foto del
modelo (Administración > Apariencia), esa foto reemplaza la figura.

La forma se deduce de cómo nombran sus referencias Hikvision y Dahua; lo que no se reconoce
cae en la figura genérica de su tipo de equipo.
"""

from __future__ import annotations

import re

FORMAS = {
    "bala": "Cámara bala",
    "domo": "Cámara domo",
    "torreta": "Cámara torreta",
    "ptz": "Cámara PTZ",
    "ojo_pez": "Cámara ojo de pez",
    "grabador": "Grabador (NVR/DVR)",
    "servidor": "Servidor",
    "panel": "Panel de control de acceso",
    "terminal": "Terminal de reconocimiento",
    "lectora": "Lectora",
    "switch": "Switch",
    "ups": "UPS",
    "otro": "Equipo",
}

_POR_TIPO = {
    "camara": "bala",
    "nvr": "grabador",
    "dvr": "grabador",
    "servidor_vms": "servidor",
    "controladora": "panel",
    "lectora": "lectora",
    "switch": "switch",
    "ups": "ups",
}

# Orden importa: lo más específico primero (HDBW antes que HDW, HFW, etc.).
_REGLAS_CAMARA = [
    (r"PTZ|^DS-2D[EFY]|^(DH-)?SD\d|^SD[0-9A-Z]", "ptz"),
    (r"FISH|OJO|^DS-2CD6[3-9]|^DS-2CD29|EBW|^(DH-)?IPC-EW", "ojo_pez"),
    (r"TURRET|TORRETA|^DS-2CD2[3H]|^DS-2CE7|(DH-)?(IPC|HAC)-HDW|EYEBALL", "torreta"),
    (r"DOM[OE]|^DS-2CD2[157]|^DS-2CE5|(DH-)?(IPC|HAC)-HD[BP]W|-HDBW|-HDPW", "domo"),
    (r"BULLET|BALA|^DS-2CD2[06T]|^DS-2CE1|(DH-)?(IPC|HAC)-HFW|-HFW|PFW", "bala"),
]
_REGLAS_ACCESO = [
    (r"^DS-K1T|^DS-K1A|^(DHI-)?ASI|FACE|ROSTRO", "terminal"),
    (r"^DS-K1(?!T|A)|^(DHI-)?ASR|LECTORA|READER", "lectora"),
    (r"^DS-K2[67]|^(DHI-)?ASC|PANEL|CONTROL", "panel"),
]


def forma(tipo_equipo: str | None, modelo: str | None) -> str:
    referencia = re.sub(r"\s+", "", (modelo or "").upper())
    if tipo_equipo == "camara":
        for patron, f in _REGLAS_CAMARA:
            if re.search(patron, referencia):
                return f
    if tipo_equipo in ("controladora", "lectora", "otro"):
        for patron, f in _REGLAS_ACCESO:
            if re.search(patron, referencia):
                return f
    return _POR_TIPO.get(tipo_equipo or "", "otro")


def clave_modelo(modelo: str | None) -> str | None:
    """La foto se guarda por referencia normalizada: 'ds-2cd2t47g2-l ' == 'DS-2CD2T47G2-L'."""
    m = re.sub(r"\s+", "", (modelo or "").upper())
    return m[:80] or None
