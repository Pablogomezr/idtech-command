"""Planificador: ejecuta cada objetivo en su intervalo y alimenta el buzón.

Flujo por chequeo: adaptador -> agregador (resumen horario) -> instantánea (si cambió
una métrica estable) -> máquina de estados (antirrebote) -> buzón (disco) -> despertar al
enviador si hubo evento.

Un adaptador que falla, se cuelga o no tiene credencial produce un resultado
DESCONOCIDO; nunca detiene a los demás.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from datetime import datetime

from ..adaptadores import Adaptador, obtener
from ..boveda import Boveda, Credencial
from .agregador import Agregador
from .buzon import Buzon
from .instantaneas import SelectorInstantaneas
from .maquina_estados import MaquinaEstados
from .modelos import Estado, EventoEstado, Objetivo, ResultadoChequeo, ahora_utc

log = logging.getLogger(__name__)

INTERVALO_MANTENIMIENTO_S = 60


class Planificador:
    def __init__(
        self,
        objetivos: list[Objetivo],
        boveda: Boveda | None,
        maquina: MaquinaEstados,
        buzon: Buzon,
        agregador: Agregador,
        al_haber_eventos: Callable[[], None],
        reloj: Callable[[], datetime] = ahora_utc,
        max_concurrencia: int = 32,
        timeout_chequeo_s: float = 30.0,
        adaptadores: dict[str, Adaptador] | None = None,
        instantaneas: SelectorInstantaneas | None = None,
    ):
        self.objetivos = objetivos
        self._boveda = boveda
        self._maquina = maquina
        self._buzon = buzon
        self._agregador = agregador
        self._al_haber_eventos = al_haber_eventos
        self._reloj = reloj
        self._semaforo = asyncio.Semaphore(max_concurrencia)
        self._timeout = timeout_chequeo_s
        self._instantaneas = instantaneas or SelectorInstantaneas()
        # Una instancia por adaptador; un nombre desconocido falla al arrancar, no en caliente.
        self._adaptadores = adaptadores or {
            nombre: obtener(nombre)() for nombre in {o.adaptador for o in objetivos}
        }

    async def chequear(self, objetivo: Objetivo) -> list[EventoEstado]:
        resultados = await self._observar(objetivo)
        momento = self._reloj()
        eventos: list[EventoEstado] = []
        propios = {objetivo.dispositivo_id, *objetivo.canales.values()}
        for r in resultados:
            if r.dispositivo_id not in propios:
                log.warning("resultado descartado: %s reportó otro dispositivo", objetivo.adaptador)
                continue
            for agregado in self._agregador.registrar(r):
                self._buzon.encolar_agregado(agregado, momento)
            if (instantanea := self._instantaneas.evaluar(r)) is not None:
                self._buzon.encolar_instantanea(instantanea, momento)
            evento = self._maquina.procesar(r)
            if evento is not None:
                self._buzon.registrar_transicion(evento, momento)
                eventos.append(evento)
                log.info(
                    "dispositivo %s [%s]: %s -> %s",
                    evento.dispositivo_id,
                    evento.chequeo,
                    evento.estado_anterior,
                    evento.estado_nuevo,
                )
        if eventos:
            self._al_haber_eventos()
        return eventos

    async def _observar(self, objetivo: Objetivo) -> list[ResultadoChequeo]:
        def desconocido(codigo: str) -> list[ResultadoChequeo]:
            return [
                ResultadoChequeo(
                    dispositivo_id=objetivo.dispositivo_id,
                    chequeo=objetivo.adaptador,
                    estado=Estado.DESCONOCIDO,
                    observado_at=self._reloj(),
                    codigo=codigo,
                )
            ]

        credencial: Credencial | None = None
        if objetivo.credencial_ref:
            try:
                if self._boveda is None:
                    raise KeyError(objetivo.credencial_ref)
                credencial = self._boveda.obtener(objetivo.credencial_ref)
            except KeyError:
                log.error("falta la credencial %r en la bóveda", objetivo.credencial_ref)
                return desconocido("credencial_no_encontrada")

        adaptador = self._adaptadores[objetivo.adaptador]
        async with self._semaforo:
            try:
                return await asyncio.wait_for(
                    adaptador.chequear(objetivo, credencial), self._timeout
                )
            except TimeoutError:
                return desconocido("tiempo_agotado")
            except Exception:
                log.exception("error en el adaptador %s", objetivo.adaptador)
                return desconocido("error_adaptador")

    async def _bucle_objetivo(self, objetivo: Objetivo) -> None:
        # Desfase inicial para repartir la carga y no sondear todo en el mismo segundo.
        await asyncio.sleep(random.uniform(0, min(objetivo.intervalo_s, 15)))
        bucle = asyncio.get_running_loop()
        while True:
            inicio = bucle.time()
            await self.chequear(objetivo)
            await asyncio.sleep(max(0.0, objetivo.intervalo_s - (bucle.time() - inicio)))

    def mantenimiento(self) -> None:
        momento = self._reloj()
        for agregado in self._agregador.cerrar_hasta(momento):
            self._buzon.encolar_agregado(agregado, momento)
        self._buzon.purgar(momento)

    async def _bucle_mantenimiento(self) -> None:
        while True:
            await asyncio.sleep(INTERVALO_MANTENIMIENTO_S)
            self.mantenimiento()

    async def ejecutar(self) -> None:
        async with asyncio.TaskGroup() as grupo:
            for objetivo in self.objetivos:
                grupo.create_task(self._bucle_objetivo(objetivo))
            grupo.create_task(self._bucle_mantenimiento())
