"""Fotos y firmas de los servicios. El contenido vive fuera de la base, direccionado por su
SHA-256 (la base guarda solo la huella): una foto no se puede cambiar sin que se note.

Laboratorio y servidor único: disco local (IDCMD_ARCHIVOS, por defecto /datos/archivos).
Producción: Object Storage región Bogotá (ADR 0002), misma interfaz.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

MAX_BYTES = 8 * 1024 * 1024
FIRMAS = {b"\xff\xd8\xff": "image/jpeg", b"\x89PNG\r\n\x1a\n": "image/png"}


class ArchivoInvalido(ValueError):
    pass


def tipo_mime(contenido: bytes) -> str:
    """Se acepta lo que el contenido ES, no lo que dice el navegador."""
    for firma, mime in FIRMAS.items():
        if contenido.startswith(firma):
            return mime
    raise ArchivoInvalido("solo se aceptan fotos JPEG o PNG")


def _raiz() -> Path:
    return Path(os.environ.get("IDCMD_ARCHIVOS", "/datos/archivos"))


def _ruta(sitio_id: uuid.UUID, sha256: bytes) -> Path:
    return _raiz() / str(sitio_id) / sha256.hex()


def guardar(sitio_id: uuid.UUID, contenido: bytes) -> tuple[str, bytes]:
    """(mime, sha256). Escritura atómica: nunca queda un archivo a medias."""
    if not contenido or len(contenido) > MAX_BYTES:
        raise ArchivoInvalido("la foto está vacía o pesa más de 8 MB")
    mime = tipo_mime(contenido)
    huella = hashlib.sha256(contenido).digest()
    destino = _ruta(sitio_id, huella)
    if not destino.exists():
        destino.parent.mkdir(parents=True, exist_ok=True)
        temporal = destino.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporal.write_bytes(contenido)
        temporal.replace(destino)
    return mime, huella


def leer(sitio_id: uuid.UUID, sha256: bytes) -> bytes:
    contenido = _ruta(sitio_id, sha256).read_bytes()
    if hashlib.sha256(contenido).digest() != sha256:  # alterado o corrupto en disco
        raise ArchivoInvalido("el archivo no coincide con su huella")
    return contenido
