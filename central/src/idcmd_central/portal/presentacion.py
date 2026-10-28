"""Textos y formatos del portal (español de Colombia, hora de Bogotá)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

ZONA = ZoneInfo("America/Bogota")

ESTADOS = {
    "ok": "En línea",
    "degradado": "Con novedad",
    "caido": "Caído",
    "desconocido": "Sin respuesta",
    "sin_datos": "Sin datos",
}

CHEQUEOS = {
    "icmp": "Red (ping)",
    "hikvision_acceso": "Panel de acceso",
    "hikvision_registro": "Auditoría de accesos",
    "dahua_acceso": "Controladora",
    "dahua_camara": "Cámara",
    "dahua_nvr": "Grabador (NVR)",
    "dahua_nvr_canal": "Canal del NVR",
    "dahua_retencion": "Retención de video",
    "dahua_registro": "Auditoría de accesos",
    "simulado": "Simulado (demo)",
}

TIPOS_EQUIPO = {
    "camara": "Cámara",
    "nvr": "Grabador (NVR)",
    "dvr": "Grabador (DVR)",
    "servidor_vms": "Servidor de video",
    "lectora": "Lectora",
    "controladora": "Controladora de acceso",
    "switch": "Switch",
    "ups": "UPS",
    "otro": "Otro",
}

# Códigos más frecuentes; el resto se muestra legible a partir del propio código.
CODIGOS = {
    "tamper_activo": "Tapa abierta (tamper)",
    "bateria_baja": "Batería baja o ausente",
    "reloj_desfasado": "Reloj desfasado",
    "zona_horaria_incorrecta": "Zona horaria incorrecta",
    "lectora_desconectada": "Lectora desconectada",
    "camara_desconectada_nvr": "Cámara desconectada del NVR",
    "canal_sin_camara": "Canal sin cámara asignada",
    "sin_grabacion_reciente": "Sin grabación reciente",
    "sin_grabaciones": "Sin grabaciones",
    "retencion_insuficiente": "Retención menor a la contratada",
    "retencion_proyectada_insuficiente": "La retención no alcanzará lo contratado",
    "disco_con_error": "Disco con error",
    "sin_disco": "Sin disco",
    "disco_casi_lleno": "Disco casi lleno",
    "capacidad_reducida": "Capacidad de almacenamiento reducida",
    "credencial_rechazada": "Clave del agente rechazada",
    "cuenta_bloqueada": "Cuenta del agente bloqueada en el equipo",
    "sin_respuesta_icmp": "No responde a ping",
    "sin_respuesta_http": "No responde",
    "tiempo_agotado": "Tiempo de espera agotado",
    "caida_simulada": "Caída simulada (demo)",
}

METRICAS = {
    "modelo": "Modelo",
    "firmware": "Firmware",
    "numero_serie": "Serie",
    "latencia_ms": "Latencia (ms)",
    "perdida_pct": "Pérdida (%)",
    "desfase_reloj_s": "Desfase del reloj (s)",
    "ntp_activo": "NTP",
    "zona_horaria_utc_h": "Zona horaria (UTC)",
    "tamper_activo": "Tamper",
    "bateria_baja": "Batería baja",
    "lectoras_desconectadas": "Lectoras desconectadas",
    "usuarios_registrados": "Usuarios registrados",
    "tarjetas_registradas": "Tarjetas registradas",
    "modos_verificacion": "Modos de verificación",
    "discos_total": "Discos",
    "discos_con_error": "Discos con error",
    "hdd_estado": "Estado del disco",
    "almacenamiento_uso_pct": "Uso del disco (%)",
    "almacenamiento_total_gb": "Capacidad (GB)",
    "almacenamiento_libre_gb": "Libre (GB)",
    "retencion_real_dias": "Retención real (días)",
    "retencion_proyectada_dias": "Retención proyectada (días)",
}

OPERACIONES = {
    "inicio_sesion": "Inicio de sesión",
    "inicio_sesion_fallido": "Inicio de sesión fallido",
    "cierre_sesion": "Cierre de sesión",
    "cambio_configuracion": "Cambio de configuración",
    "busqueda_video": "Búsqueda de video",
    "reproduccion_video": "Reproducción de video",
    "exportacion_video": "Exportación de video",
    "borrado_registros": "Borrado de registros",
    "reinicio": "Reinicio",
    "actualizacion_firmware": "Actualización de firmware",
    "otra": "Otra",
}
CRITICAS = {
    "inicio_sesion_fallido",
    "cambio_configuracion",
    "exportacion_video",
    "borrado_registros",
    "reinicio",
    "actualizacion_firmware",
}


def legible(codigo: str | None) -> str:
    if not codigo:
        return ""
    return codigo.replace("_", " ").capitalize()


def nombre_codigo(codigo: str | None) -> str:
    return CODIGOS.get(codigo or "", legible(codigo))


def hora(momento: datetime | None, formato: str = "%Y-%m-%d %H:%M") -> str:
    return momento.astimezone(ZONA).strftime(formato) if momento else "—"


def hace(momento: datetime | None, ahora: datetime) -> str:
    if momento is None:
        return "nunca"
    s = int((ahora - momento).total_seconds())
    if s < 90:
        return "hace un momento"
    if s < 3600:
        return f"hace {s // 60} min"
    if s < 86400:
        return f"hace {s // 3600} h"
    return f"hace {s // 86400} días"


def valor(v) -> str:
    if isinstance(v, bool):
        return "sí" if v else "no"
    if isinstance(v, float):
        return f"{v:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return str(v)


ESTADOS_TICKET = {
    "abierto": "Abierto",
    "asignado": "Asignado",
    "aceptado": "Aceptado",
    "en_camino": "En camino",
    "en_sitio": "En sitio",
    "resuelto": "Resuelto (verificando)",
    "cerrado": "Cerrado",
    "cancelado": "Cancelado",
}
PRIORIDADES = {"critica": "Crítica", "alta": "Alta", "media": "Media", "baja": "Baja"}
CIERRES = {
    "verificado": "Reparado y verificado",
    "recuperado_solo": "Se recuperó sin intervención",
    "cancelado": "Cancelado",
}
# Botones del técnico según el estado del ticket: (acción, texto).
ACCIONES_TICKET = {
    "abierto": [("tomar", "Tomar este servicio")],
    "asignado": [("aceptar", "Aceptar"), ("tomar", "Tomar este servicio")],
    "aceptado": [("en_camino", "Voy en camino"), ("en_sitio", "Llegué al sitio"),
                 ("resuelto", "Resuelto")],
    "en_camino": [("en_sitio", "Llegué al sitio"), ("resuelto", "Resuelto")],
    "en_sitio": [("resuelto", "Resuelto")],
}  # fmt: skip


def duracion(segundos: float) -> str:
    s = int(abs(segundos))
    if s < 3600:
        return f"{max(s // 60, 1)} min"
    if s < 86400:
        h, m = divmod(s // 60, 60)
        return f"{h} h {m} min" if m else f"{h} h"
    d, h = divmod(s // 3600, 24)
    return f"{d} d {h} h" if h else f"{d} d"


def plazo(vence, cumplido, ahora) -> tuple[str, str]:
    """(texto, nivel) de un compromiso SLA: cumplido a tiempo, vencido o cuánto falta."""
    if cumplido is not None:
        ok = cumplido <= vence
        return ("Cumplido" if ok else "Cumplido fuera de plazo"), ("bien" if ok else "mal")
    falta = (vence - ahora).total_seconds()
    if falta < 0:
        return f"Vencido hace {duracion(falta)}", "mal"
    return f"Vence en {duracion(falta)}", "atencion" if falta < 3600 else "bien"
