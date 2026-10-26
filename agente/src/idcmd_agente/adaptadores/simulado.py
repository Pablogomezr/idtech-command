"""Adaptador simulado: caídas ficticias y reproducibles para demos y pruebas.

Sirve para mostrarle al cliente el portal y el informe antes de instalar nada
en su red. Nunca usarlo en un sitio real: sus datos no son mediciones.

Parámetros del objetivo:
    prob_caida              probabilidad de iniciar una caída en cada chequeo (0.01)
    duracion_caida_s        duración media de una caída en segundos (600)
    retencion_contratada_dias  si se indica, reporta una retención real ficticia
    semilla                 semilla para reproducir el mismo escenario (0)
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import ClassVar

from ..boveda import Credencial
from ..nucleo.modelos import Estado, Objetivo, ResultadoChequeo, ahora_utc
from .base import registrar

CHEQUEO = "simulado"


@registrar("simulado")
class AdaptadorSimulado:
    nombre: ClassVar[str]
    capacidades: ClassVar[frozenset[str]] = frozenset({"disponibilidad", "retencion"})

    def __init__(self, reloj: Callable[[], datetime] = ahora_utc):
        self._reloj = reloj
        self._azar: dict[uuid.UUID, random.Random] = {}
        self._fin_caida: dict[uuid.UUID, datetime] = {}
        self._retencion: dict[uuid.UUID, float] = {}

    async def chequear(
        self, objetivo: Objetivo, credencial: Credencial | None
    ) -> list[ResultadoChequeo]:
        p = objetivo.parametros
        ahora = self._reloj()
        disp = objetivo.dispositivo_id
        azar = self._azar.setdefault(disp, random.Random(f"{p.get('semilla', 0)}:{disp}"))

        def resultado(estado: Estado, codigo: str | None = None, **metricas) -> ResultadoChequeo:
            return ResultadoChequeo(
                dispositivo_id=disp,
                chequeo=CHEQUEO,
                estado=estado,
                observado_at=ahora,
                codigo=codigo,
                metricas=metricas,
            )

        fin = self._fin_caida.get(disp)
        if fin is not None and ahora < fin:
            return [resultado(Estado.CAIDO, "caida_simulada")]
        if azar.random() < float(p.get("prob_caida", 0.01)):
            duracion = float(p.get("duracion_caida_s", 600))
            self._fin_caida[disp] = ahora + timedelta(seconds=azar.uniform(0.5, 1.5) * duracion)
            return [resultado(Estado.CAIDO, "caida_simulada")]

        metricas: dict = {"latencia_ms": round(azar.uniform(1, 20), 2)}
        contratada = p.get("retencion_contratada_dias")
        if contratada is not None:
            real = self._retencion.setdefault(
                disp, round(float(contratada) * azar.uniform(0.85, 1.1), 1)
            )
            metricas["retencion_real_dias"] = real
            if real < float(contratada):
                return [resultado(Estado.DEGRADADO, "retencion_insuficiente", **metricas)]
        return [resultado(Estado.OK, **metricas)]
