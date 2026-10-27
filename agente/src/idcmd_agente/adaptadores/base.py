"""Contrato de los adaptadores y su registro.

Para agregar una marca: crear `adaptadores/<marca>.py` con una clase decorada
con `@registrar("<marca>")`. El núcleo descubre el módulo solo; no hay que
tocar nada más. Un paquete externo también puede aportar adaptadores
declarando el entry point `idcmd_agente.adaptadores`.

Reglas para todo adaptador:

* Solo lectura: usar `ClienteSoloLectura` con una lista blanca de operaciones.
* Traducir lo que reporta el fabricante al vocabulario común (`METRICAS_PERMITIDAS`).
* Nunca incluir la credencial ni datos de personas en los resultados.
* Puede devolver también `OperacionEquipo` (registro de operaciones del equipo, ADR 0009).
"""

from __future__ import annotations

import importlib
import pkgutil
from importlib.metadata import entry_points
from typing import ClassVar, Protocol, runtime_checkable

from ..boveda import Credencial
from ..nucleo.modelos import Objetivo, OperacionEquipo, ResultadoChequeo, validar_codigo

GRUPO_ENTRY_POINTS = "idcmd_agente.adaptadores"


@runtime_checkable
class Adaptador(Protocol):
    nombre: ClassVar[str]
    capacidades: ClassVar[frozenset[str]]

    async def chequear(
        self, objetivo: Objetivo, credencial: Credencial | None
    ) -> list[ResultadoChequeo | OperacionEquipo]: ...


_REGISTRO: dict[str, type[Adaptador]] = {}


class AdaptadorDesconocido(LookupError):
    pass


def registrar(nombre: str):
    validar_codigo(nombre)

    def decorador(cls: type) -> type:
        if nombre in _REGISTRO and _REGISTRO[nombre] is not cls:
            raise ValueError(f"ya existe un adaptador registrado como {nombre!r}")
        cls.nombre = nombre
        _REGISTRO[nombre] = cls
        return cls

    return decorador


def obtener(nombre: str) -> type[Adaptador]:
    try:
        return _REGISTRO[nombre]
    except KeyError:
        disponibles = ", ".join(sorted(_REGISTRO)) or "ninguno"
        raise AdaptadorDesconocido(
            f"adaptador {nombre!r} no registrado (disponibles: {disponibles})"
        ) from None


def registrados() -> list[str]:
    return sorted(_REGISTRO)


def descubrir() -> list[str]:
    """Importa los módulos de este paquete y los entry points externos."""
    from .. import adaptadores

    for modulo in pkgutil.iter_modules(adaptadores.__path__):
        if not modulo.name.startswith("_") and modulo.name not in {"base", "http_seguro"}:
            importlib.import_module(f"{adaptadores.__name__}.{modulo.name}")
    for ep in entry_points(group=GRUPO_ENTRY_POINTS):
        ep.load()
    return registrados()
