"""Ensamble del agente: conecta configuración, bóveda, buzón, planificador y enviador."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import uuid

from . import __version__
from .adaptadores import descubrir, registrados
from .boveda import Boveda
from .config import ConfigAgente
from .nucleo.agregador import Agregador
from .nucleo.buzon import Buzon
from .nucleo.enviador import ConfigEnvio, Enviador
from .nucleo.maquina_estados import MaquinaEstados
from .nucleo.modelos import Latido, ahora_utc
from .nucleo.planificador import Planificador
from .nucleo.seguridad import GuardaSecretos, configurar_logs

log = logging.getLogger(__name__)


async def ejecutar_agente(config: ConfigAgente) -> None:
    guarda = GuardaSecretos()
    configurar_logs(guarda)
    inicio = ahora_utc()

    token = config.leer_token()
    guarda.registrar(token)
    boveda = Boveda.desde_archivos(
        config.almacenamiento.boveda, config.almacenamiento.archivo_llave, guarda
    )
    buzon = Buzon(config.almacenamiento.buzon, guarda)
    maquina = MaquinaEstados(
        config.deteccion.confirmaciones_falla, config.deteccion.confirmaciones_recuperacion
    )
    maquina.cargar(buzon.estados())
    agregador = Agregador(sesion=uuid.uuid4())
    descubrir()

    def latido() -> Latido:
        return Latido(
            agente_version=__version__,
            reloj_agente=ahora_utc(),
            inicio_sesion=inicio,
            pendientes=buzon.pendientes(),
            adaptadores=registrados(),
        )

    enviador = Enviador(
        ConfigEnvio(
            url_central=config.central.url,
            token=token,
            agente_id=config.agente_id,
            ca_adicional=config.central.ca_adicional,
        ),
        buzon,
        latido,
    )
    planificador = Planificador(
        config.objetivos, boveda, maquina, buzon, agregador, enviador.despertar
    )
    log.info(
        "agente %s v%s: %d objetivos, adaptadores %s",
        config.agente_id,
        __version__,
        len(config.objetivos),
        ", ".join(registrados()),
    )

    tarea = asyncio.ensure_future(asyncio.gather(planificador.ejecutar(), enviador.ejecutar()))
    bucle = asyncio.get_running_loop()
    for senal in (signal.SIGTERM, signal.SIGINT):
        bucle.add_signal_handler(senal, tarea.cancel)
    try:
        await tarea
    except asyncio.CancelledError:
        log.info("deteniendo el agente")
    finally:
        # La hora en curso se guarda como agregado parcial; se envía al volver.
        momento = ahora_utc()
        for agregado in agregador.vaciar():
            buzon.encolar_agregado(agregado, momento)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(enviador.enviar_pendientes(), 10)
        await enviador.cerrar()
        buzon.cerrar()
