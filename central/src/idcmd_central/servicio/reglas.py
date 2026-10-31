"""Qué fallas abren ticket solas, con qué prioridad, y cómo se arma el enlace del cliente.

Decisión de Pablo (2026-10-28): caídas, cámaras desconectadas o sin grabar, discos, tamper y
batería abren un ticket correctivo; las novedades menores (reloj, zona horaria, NTP) un
ticket preventivo de prioridad baja.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import uuid

ORDEN = ["baja", "media", "alta", "critica"]

# codigo -> (prioridad base, tipo). Lo que no está aquí no abre ticket.
REGLAS: dict[str, tuple[str, str]] = {
    "sitio_sin_conexion": ("critica", "correctivo"),
    "sin_respuesta_icmp": ("alta", "correctivo"),
    "sin_respuesta_http": ("alta", "correctivo"),
    "camara_desconectada_nvr": ("alta", "correctivo"),
    "canal_sin_camara": ("alta", "correctivo"),
    "sin_grabacion_reciente": ("alta", "correctivo"),
    "sin_grabaciones": ("alta", "correctivo"),
    "sin_disco": ("critica", "correctivo"),
    "disco_con_error": ("alta", "correctivo"),
    "capacidad_reducida": ("alta", "correctivo"),
    "tamper_activo": ("alta", "correctivo"),
    "bateria_baja": ("alta", "correctivo"),
    "lectora_desconectada": ("alta", "correctivo"),
    "perdida_paquetes": ("media", "correctivo"),
    "retencion_insuficiente": ("media", "correctivo"),
    "retencion_proyectada_insuficiente": ("media", "preventivo"),
    "credencial_rechazada": ("media", "correctivo"),
    "cuenta_bloqueada": ("media", "correctivo"),
    "caida_simulada": ("alta", "correctivo"),  # adaptador `simulado` (demos)
    "reloj_desfasado": ("baja", "preventivo"),
    "zona_horaria_incorrecta": ("baja", "preventivo"),
}

# Un grabador caído deja sin video a todas sus cámaras: sube un nivel.
TIPOS_QUE_SUBEN = {"nvr", "dvr", "servidor_vms"}


def prioridad(codigo: str, tipo_equipo: str | None, criticidad: int | None) -> tuple[str, str]:
    base, tipo = REGLAS[codigo]
    nivel = ORDEN.index(base)
    if tipo == "correctivo" and (criticidad == 1 or tipo_equipo in TIPOS_QUE_SUBEN):
        nivel = min(nivel + 1, len(ORDEN) - 1)
    return ORDEN[nivel], tipo


def mas_grave(a: str, b: str) -> str:
    return a if ORDEN.index(a) >= ORDEN.index(b) else b


# --- Enlace de seguimiento del cliente ------------------------------------------------------


def _llave() -> bytes:
    # Derivada de la llave del servidor: no hace falta otro secreto, y no se guarda en la base.
    base = base64.b64decode(os.environ["IDCMD_LLAVE_TOTP"])
    return hmac.new(base, b"idcmd-seguimiento-v1", hashlib.sha256).digest()


def firma_seguimiento(ticket_id: uuid.UUID) -> str:
    return hmac.new(_llave(), ticket_id.bytes, hashlib.sha256).hexdigest()[:32]


def firma_valida(ticket_id: uuid.UUID, firma: str) -> bool:
    return hmac.compare_digest(firma_seguimiento(ticket_id), firma)


def firma_informe(sitio_id: uuid.UUID, periodo: str) -> str:
    """Enlace del informe mensual para el cliente: firmado por sitio y mes ('2026-10')."""
    llave = hmac.new(_llave(), b"idcmd-informe-v1", hashlib.sha256).digest()
    return hmac.new(llave, sitio_id.bytes + periodo.encode(), hashlib.sha256).hexdigest()[:32]


def firma_informe_valida(sitio_id: uuid.UUID, periodo: str, firma: str) -> bool:
    return hmac.compare_digest(firma_informe(sitio_id, periodo), firma)


def url_publica() -> str:
    return os.environ.get("IDCMD_URL_PUBLICA", "http://localhost:8000").rstrip("/")


def enlace_seguimiento(ticket_id: uuid.UUID) -> str:
    return f"{url_publica()}/seguimiento/{ticket_id}/{firma_seguimiento(ticket_id)}"


def enlace_portal(ticket_id: uuid.UUID) -> str:
    return f"{url_publica()}/portal/tickets/{ticket_id}"


def enlace_informe(sitio_id: uuid.UUID, periodo: str) -> str:
    return f"{url_publica()}/informe/{sitio_id}/{periodo}/{firma_informe(sitio_id, periodo)}"
