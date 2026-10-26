"""Bóveda local de credenciales cifradas. Las credenciales nunca salen del agente."""

from .almacen import Boveda, BovedaError, Credencial

__all__ = ["Boveda", "BovedaError", "Credencial"]
