"""Traduce las métricas de cada chequeo a lo que un operador necesita: una frase, una lista de
verificaciones (✓/✗), barras para lo que se mide en proporción y qué hacer si algo falla.

Los números crudos no se pierden: van a "Detalles técnicos" (plegado) para el ingeniero.
Regla: el operador nunca debe tener que saber qué es `desfase_reloj_s` para entender que el
reloj del panel está atrasado y por qué importa.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .presentacion import METRICAS, nombre_codigo, valor


@dataclass
class Punto:
    """Una verificación: ✓ (bien), ! (atención) o ✗ (mal), con su explicación."""

    nivel: str  # "bien" | "atencion" | "mal" | "info"
    texto: str


@dataclass
class Barra:
    etiqueta: str
    porcentaje: float  # 0..100 del ancho de la barra
    nivel: str  # "bien" | "atencion" | "mal"
    texto: str


@dataclass
class Lectura:
    titular: str
    puntos: list[Punto] = field(default_factory=list)
    barras: list[Barra] = field(default_factory=list)
    acciones: list[str] = field(default_factory=list)
    tecnicos: dict[str, str] = field(default_factory=dict)


# Qué hacer, en una frase, ante las fallas más comunes (la base de conocimiento completa
# llega con los tickets en la Etapa 3).
ACCIONES = {
    "sin_respuesta_icmp": "Verificar energía, cable de red y puerto del switch (PoE).",
    "sin_respuesta_http": "Verificar que el equipo esté encendido y en la red.",
    "perdida_paquetes": "Revisar cable, conector y puerto del switch: la conexión es inestable.",
    "camara_desconectada_nvr": "Revisar energía (PoE) y cable de la cámara; luego el canal en "
    "el NVR.",
    "canal_sin_camara": "Alguien quitó o movió la cámara en el NVR: revisar la pestaña "
    "Auditoría y reasignarla al canal contratado.",
    "sin_grabacion_reciente": "La cámara no está grabando: revisar la programación de grabación "
    "del canal y el estado del disco.",
    "sin_grabaciones": "No hay video de esta cámara: revisar la programación de grabación.",
    "retencion_insuficiente": "El video no alcanza los días contratados: ampliar disco o ajustar "
    "calidad de grabación.",
    "retencion_proyectada_insuficiente": "Al llenarse el disco no alcanzarán los días "
    "contratados: planear ampliación de disco.",
    "disco_con_error": "Disco con falla: programar cambio de disco y respaldar lo urgente.",
    "sin_disco": "El grabador no detecta disco: no está grabando. Atender de inmediato.",
    "capacidad_reducida": "El grabador tiene menos capacidad que la instalada: un disco falló o se "
    "retiró.",
    "tamper_activo": "La tapa del panel está abierta: cerrarla y verificar que nadie haya "
    "manipulado el cableado.",
    "bateria_baja": "Sin batería de respaldo: si se va la luz, el panel se apaga. Instalar o "
    "cambiar la batería.",
    "lectora_desconectada": "Revisar cable RS-485 y alimentación de la lectora.",
    "reloj_desfasado": "Activar la sincronización de hora (NTP) en el equipo: con el reloj "
    "desfasado, las marcaciones y grabaciones pierden valor como evidencia.",
    "zona_horaria_incorrecta": "Corregir la zona horaria del equipo a Bogotá (UTC-5).",
    "credencial_rechazada": "El equipo rechazó la clave del agente: actualizarla en la bóveda "
    "del agente (el agente no insistirá por 30 min para no bloquear la cuenta).",
    "cuenta_bloqueada": "El equipo bloqueó la cuenta del agente: esperar el desbloqueo y "
    "verificar la clave guardada.",
}

_MODOS = {7: "huella o tarjeta"}  # códigos confirmados en campo; el resto se muestra crudo


def _tecnicos(m: dict, *claves: str) -> dict[str, str]:
    return {METRICAS.get(k, k): valor(m[k]) for k in claves if k in m}


def _reloj(m: dict) -> list[Punto]:
    puntos = []
    desfase = m.get("desfase_reloj_s")
    if isinstance(desfase, int | float):
        s = abs(desfase)
        sentido = "atrasado" if desfase < 0 else "adelantado"
        if s < 30:
            puntos.append(Punto("bien", "Hora correcta"))
        else:
            texto = f"{int(s // 60)} min {int(s % 60)} s" if s >= 60 else f"{int(s)} s"
            puntos.append(Punto("mal" if s > 120 else "atencion", f"Reloj {sentido} {texto}"))
    if m.get("ntp_activo") is False:
        puntos.append(
            Punto("atencion", "Sincronización de hora (NTP) apagada: el reloj se irá desviando")
        )
    zona = m.get("zona_horaria_utc_h")
    if isinstance(zona, int | float) and zona != -5:
        puntos.append(Punto("mal", f"Zona horaria UTC{zona:+g}; en Colombia debe ser UTC-5"))
    return puntos


def _ping(m: dict, estado: str) -> Lectura:
    if estado == "ok":
        latencia = m.get("latencia_ms")
        rapidez = "" if latencia is None else (" (conexión rápida)" if latencia < 50 else "")
        return Lectura(
            f"Responde en la red{rapidez}", tecnicos=_tecnicos(m, "latencia_ms", "perdida_pct")
        )
    perdida = m.get("perdida_pct")
    if estado == "degradado" and perdida:
        return Lectura(
            f"Conexión inestable: se pierde el {valor(perdida)} % de las respuestas",
            tecnicos=_tecnicos(m, "latencia_ms", "perdida_pct"),
        )
    return Lectura("No responde en la red", tecnicos=_tecnicos(m, "latencia_ms", "perdida_pct"))


def _nvr(m: dict, estado: str, codigo: str | None) -> Lectura:
    lectura = Lectura(
        "Grabador funcionando" if estado == "ok" else nombre_codigo(codigo) or "Revisar grabador",
        tecnicos=_tecnicos(
            m, "modelo", "firmware", "numero_serie", "almacenamiento_total_gb",
            "almacenamiento_libre_gb", "hdd_estado", "latencia_ms", "desfase_reloj_s",
        ),
    )  # fmt: skip
    uso = m.get("almacenamiento_uso_pct")
    if isinstance(uso, int | float):
        total = m.get("almacenamiento_total_gb")
        capacidad = f" de {total / 1000:.1f} TB".replace(".", ",") if total else ""
        lectura.barras.append(
            Barra(
                "Disco",
                min(uso, 100),
                "bien",  # un NVR sobrescribe lo más viejo: lleno es lo normal
                f"{valor(float(uso))} % usado{capacidad}",
            )
        )
    errores = m.get("discos_con_error")
    if isinstance(errores, int):
        total_discos = m.get("discos_total")
        discos = f"{total_discos} disco(s)" if total_discos else "Discos"
        lectura.puntos.append(
            Punto("bien", f"{discos} sin errores")
            if errores == 0
            else Punto("mal", f"{errores} disco(s) con error")
        )
    lectura.puntos += _reloj(m)
    return lectura


def _retencion(m: dict, estado: str, codigo: str | None, contratada: int | None) -> Lectura:
    real = m.get("retencion_real_dias")
    proyectada = m.get("retencion_proyectada_dias")
    if codigo == "sin_grabacion_reciente":
        titular = "No está grabando: no hay video de las últimas horas"
    elif codigo:
        titular = nombre_codigo(codigo)
    elif isinstance(real, int | float):
        titular = f"Guarda {valor(float(real))} días de video"
    else:
        titular = "Sin datos de retención todavía"
    lectura = Lectura(
        titular, tecnicos=_tecnicos(m, "retencion_real_dias", "retencion_proyectada_dias")
    )
    if isinstance(real, int | float) and contratada:
        # Video viejo no es cumplimiento si la cámara ya no graba: la barra no se pinta verde.
        cumple = real >= contratada and codigo != "sin_grabacion_reciente"
        lectura.barras.append(
            Barra(
                "Video guardado",
                min(real / contratada * 100, 100),
                "bien" if cumple else "atencion",
                f"{valor(float(real))} de {contratada} días contratados" + (" ✓" if cumple else ""),
            )
        )
    if isinstance(proyectada, int | float):
        lectura.puntos.append(
            Punto(
                "bien" if not contratada or proyectada >= contratada else "atencion",
                f"Cuando el disco se llene alcanzará unos {round(proyectada)} días",
            )
        )
    if codigo != "sin_grabacion_reciente" and estado != "desconocido":
        lectura.puntos.append(Punto("bien", "Grabando: hay video reciente"))
    return lectura


def _panel(m: dict, estado: str, codigo: str | None) -> Lectura:
    puntos: list[Punto] = []
    if "tamper_activo" in m:
        puntos.append(
            Punto("mal", "Tapa abierta (tamper)")
            if m["tamper_activo"]
            else Punto("bien", "Tapa cerrada")
        )
    if "bateria_baja" in m:
        puntos.append(
            Punto("mal", "Sin batería de respaldo o batería baja")
            if m["bateria_baja"]
            else Punto("bien", "Batería de respaldo bien")
        )
    if "lectoras_desconectadas" in m:
        n = m["lectoras_desconectadas"]
        puntos.append(
            Punto("bien", "Todas las lectoras conectadas")
            if n == 0
            else Punto("mal", f"{n} lectora(s) desconectada(s)")
        )
    puntos += _reloj(m)
    usuarios, tarjetas = m.get("usuarios_registrados"), m.get("tarjetas_registradas")
    if usuarios is not None or tarjetas is not None:
        partes = []
        if usuarios is not None:
            partes.append(f"{usuarios} usuario(s)")
        if tarjetas is not None:
            partes.append(f"{tarjetas} tarjeta(s)")
        puntos.append(Punto("info", " y ".join(partes) + " registrados"))
    modos = m.get("modos_verificacion")
    if isinstance(modos, str) and modos:
        codigos = {int(x) for x in modos.split(",") if x.strip().isdigit()}
        if len(codigos) == 1 and (nombre := _MODOS.get(next(iter(codigos)))):
            puntos.append(Punto("info", f"Se entra con {nombre}"))
    malos = [p for p in puntos if p.nivel == "mal"]
    if estado == "ok":
        titular = "Panel funcionando"
    elif len(malos) > 1:
        titular = f"{len(malos)} novedades en el panel"
    else:
        titular = malos[0].texto if malos else (nombre_codigo(codigo) or "Revisar panel")
    return Lectura(
        titular,
        puntos=puntos,
        tecnicos=_tecnicos(
            m, "modelo", "firmware", "numero_serie", "latencia_ms", "desfase_reloj_s",
            "zona_horaria_utc_h", "modos_verificacion",
        ),
    )  # fmt: skip


def interpretar(
    chequeo: str,
    estado: str | None,
    codigo: str | None,
    metricas: dict,
    contratada: int | None = None,
) -> Lectura:
    estado = estado or "sin_datos"
    m = metricas or {}
    if chequeo == "icmp":
        lectura = _ping(m, estado)
    elif chequeo == "dahua_nvr":
        lectura = _nvr(m, estado, codigo)
    elif chequeo == "dahua_nvr_canal":
        lectura = Lectura(
            "Conectada al grabador" if estado == "ok" else nombre_codigo(codigo) or "Revisar canal"
        )
    elif chequeo == "dahua_retencion":
        lectura = _retencion(m, estado, codigo, contratada)
    elif chequeo in ("hikvision_acceso", "dahua_acceso"):
        lectura = _panel(m, estado, codigo)
    elif chequeo in ("dahua_registro", "hikvision_registro"):
        lectura = Lectura(
            "Auditoría de accesos activa" if estado == "ok" else nombre_codigo(codigo)
            or "Auditoría sin datos",
        )  # fmt: skip
    else:
        lectura = Lectura(
            "Sin novedad" if estado == "ok" else nombre_codigo(codigo) or "Revisar",
            tecnicos={METRICAS.get(k, k): valor(v) for k, v in m.items()},
        )
    if estado not in ("ok", "sin_datos"):
        if codigo in ACCIONES:
            lectura.acciones.append(ACCIONES[codigo])
        # Un panel con varias novedades: una acción por cada problema concreto.
        if chequeo in ("hikvision_acceso", "dahua_acceso"):
            for clave, cod in (
                ("tamper_activo", "tamper_activo"),
                ("bateria_baja", "bateria_baja"),
                ("lectoras_desconectadas", "lectora_desconectada"),
            ):
                if m.get(clave) and ACCIONES[cod] not in lectura.acciones:
                    lectura.acciones.append(ACCIONES[cod])
    # El reloj se informa aunque el equipo esté "ok": es evidencia para el cliente.
    if (
        any(
            p.texto.startswith(("Reloj", "Sincronización")) and p.nivel != "bien"
            for p in lectura.puntos
        )
        and ACCIONES["reloj_desfasado"] not in lectura.acciones
    ):
        lectura.acciones.append(ACCIONES["reloj_desfasado"])
    return lectura
