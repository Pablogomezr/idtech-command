"""Disponibilidad por ICMP (ping), sin privilegios de root.

Usa sockets ICMP no privilegiados: en el contenedor se habilitan con el sysctl
`net.ipv4.ping_group_range`, sin necesidad de la capacidad NET_RAW.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

import icmplib

from ..boveda import Credencial
from ..nucleo.modelos import Estado, Objetivo, ResultadoChequeo
from .base import registrar

CHEQUEO = "icmp"


@registrar("icmp")
class AdaptadorIcmp:
    nombre: ClassVar[str]
    capacidades: ClassVar[frozenset[str]] = frozenset({"disponibilidad"})

    def __init__(self, ping: Callable[..., Awaitable[Any]] | None = None):
        self._ping = ping or icmplib.async_ping

    async def chequear(
        self, objetivo: Objetivo, credencial: Credencial | None
    ) -> list[ResultadoChequeo]:
        intentos = int(objetivo.parametros.get("intentos", 3))
        umbral_degradado = float(objetivo.parametros.get("perdida_degradado_pct", 50))

        def resultado(estado: Estado, codigo: str | None = None, **metricas) -> ResultadoChequeo:
            return ResultadoChequeo(
                dispositivo_id=objetivo.dispositivo_id,
                chequeo=CHEQUEO,
                estado=estado,
                codigo=codigo,
                metricas=metricas,
            )

        try:
            host = await self._ping(
                objetivo.direccion, count=intentos, interval=0.2, timeout=1.0, privileged=False
            )
        except icmplib.NameLookupError:
            return [resultado(Estado.DESCONOCIDO, "nombre_no_resuelto")]
        except icmplib.SocketPermissionError:
            return [resultado(Estado.DESCONOCIDO, "sin_permiso_icmp")]
        except icmplib.ICMPLibError:
            return [resultado(Estado.DESCONOCIDO, "error_icmp")]

        perdida = round(host.packet_loss * 100, 1)
        if not host.is_alive:
            return [resultado(Estado.CAIDO, "sin_respuesta_icmp", perdida_pct=perdida)]
        metricas = {"latencia_ms": round(host.avg_rtt, 2), "perdida_pct": perdida}
        if perdida >= umbral_degradado:
            return [resultado(Estado.DEGRADADO, "perdida_paquetes", **metricas)]
        return [resultado(Estado.OK, **metricas)]
