"""Adaptadores de recolección. Cada marca o protocolo vive en su propio módulo."""

from .base import Adaptador, AdaptadorDesconocido, descubrir, obtener, registrados, registrar

__all__ = ["Adaptador", "AdaptadorDesconocido", "descubrir", "obtener", "registrados", "registrar"]
