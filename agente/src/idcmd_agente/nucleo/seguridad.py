"""Última barrera contra fugas de secretos.

La bóveda y el token del agente registran aquí sus valores. Antes de que algo
entre al buzón (y por tanto pueda viajar a la central) o se escriba en un log,
se revisa que no contenga ninguno de ellos.
"""

from __future__ import annotations

import json
import logging

LONGITUD_MINIMA_SECRETO = 4
MASCARA = "***"


class SecretoDetectado(Exception):
    """Se intentó persistir o enviar un texto que contiene un secreto conocido."""


class GuardaSecretos:
    def __init__(self) -> None:
        self._variantes: set[str] = set()

    def registrar(self, valor: str) -> None:
        if len(valor) < LONGITUD_MINIMA_SECRETO:
            raise ValueError(
                f"secreto demasiado corto para vigilarlo (mín. {LONGITUD_MINIMA_SECRETO})"
            )
        self._variantes.add(valor)
        # Forma en que aparecería dentro de un JSON (comillas y barras escapadas).
        self._variantes.add(json.dumps(valor)[1:-1])

    def contiene_secreto(self, texto: str) -> bool:
        return any(v in texto for v in self._variantes)

    def verificar(self, texto: str) -> None:
        if self.contiene_secreto(texto):
            raise SecretoDetectado("el contenido incluye un secreto conocido; se descarta")

    def redactar(self, texto: str) -> str:
        # Las variantes más largas primero, para no dejar restos parciales.
        for v in sorted(self._variantes, key=len, reverse=True):
            texto = texto.replace(v, MASCARA)
        return texto


class FiltroRedaccion(logging.Filter):
    """Reemplaza secretos conocidos en los mensajes de log, incluidas las trazas."""

    def __init__(self, guarda: GuardaSecretos) -> None:
        super().__init__()
        self._guarda = guarda

    def filter(self, record: logging.LogRecord) -> bool:
        mensaje = record.getMessage()
        if record.exc_info:
            mensaje = f"{mensaje}\n{logging.Formatter().formatException(record.exc_info)}"
            record.exc_info = None
            record.exc_text = None
        record.msg = self._guarda.redactar(mensaje)
        record.args = None
        return True


def configurar_logs(guarda: GuardaSecretos, nivel: int = logging.INFO) -> None:
    manejador = logging.StreamHandler()
    manejador.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    manejador.addFilter(FiltroRedaccion(guarda))
    raiz = logging.getLogger()
    raiz.handlers[:] = [manejador]
    raiz.setLevel(nivel)
