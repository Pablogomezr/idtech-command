"""Bóveda de credenciales cifrada con AES-256-GCM.

Formato del archivo (JSON, permisos 0600):

    {"version": 1, "entradas": {"<ref>": {"n": "<nonce b64>", "c": "<cifrado b64>"}}}

La referencia (`ref`) se usa como dato autenticado (AAD): mover un cifrado de
una entrada a otra se detecta al descifrar. La llave vive en un archivo aparte
(Docker secret) sobre el disco cifrado con LUKS.

Alcance honesto: esto protege contra el robo del disco o de una copia del
archivo, no contra un atacante con root en el mini PC.
"""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, SecretStr

from ..nucleo.seguridad import LONGITUD_MINIMA_SECRETO, GuardaSecretos

PATRON_REF = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_VERSION = 1


class BovedaError(Exception):
    pass


class Credencial(BaseModel):
    """Credencial de un equipo. `clave` nunca se imprime (SecretStr)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    usuario: str
    clave: SecretStr


def _validar_ref(ref: str) -> str:
    if not PATRON_REF.fullmatch(ref):
        raise BovedaError(f"referencia inválida (use [a-z0-9_-], máx. 64): {ref!r}")
    return ref


def _aad(ref: str) -> bytes:
    return f"idcmd:{ref}".encode()


class Boveda:
    def __init__(self, ruta: Path | str, llave: bytes, guarda: GuardaSecretos | None = None):
        if len(llave) != 32:
            raise BovedaError("la llave debe tener 32 bytes (AES-256)")
        self._ruta = Path(ruta)
        self._aes = AESGCM(llave)
        self._guarda = guarda
        if self._guarda is not None:
            for ref in self.listar():
                self._guarda.registrar(self.obtener(ref).clave.get_secret_value())

    # --- llave -----------------------------------------------------------

    @staticmethod
    def generar_llave() -> bytes:
        return AESGCM.generate_key(bit_length=256)

    @staticmethod
    def escribir_llave(ruta: Path | str, llave: bytes) -> None:
        ruta = Path(ruta)
        if ruta.exists():
            raise BovedaError(f"ya existe {ruta}; no se sobrescribe una llave")
        descriptor = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as f:
            f.write(base64.b64encode(llave).decode() + "\n")

    @staticmethod
    def leer_llave(ruta: Path | str) -> bytes:
        try:
            return base64.b64decode(Path(ruta).read_text().strip(), validate=True)
        except (OSError, ValueError) as e:
            raise BovedaError(f"no se pudo leer la llave en {ruta}") from e

    @classmethod
    def desde_archivos(
        cls, ruta: Path | str, ruta_llave: Path | str, guarda: GuardaSecretos | None = None
    ) -> Boveda:
        return cls(ruta, cls.leer_llave(ruta_llave), guarda)

    # --- operaciones -----------------------------------------------------

    def guardar(self, ref: str, credencial: Credencial) -> None:
        _validar_ref(ref)
        clave = credencial.clave.get_secret_value()
        if len(clave) < LONGITUD_MINIMA_SECRETO:
            raise BovedaError(f"la clave debe tener al menos {LONGITUD_MINIMA_SECRETO} caracteres")
        claro = json.dumps({"usuario": credencial.usuario, "clave": clave}).encode()
        nonce = os.urandom(12)
        cifrado = self._aes.encrypt(nonce, claro, _aad(ref))
        datos = self._leer()
        datos["entradas"][ref] = {
            "n": base64.b64encode(nonce).decode(),
            "c": base64.b64encode(cifrado).decode(),
        }
        self._escribir(datos)
        if self._guarda is not None:
            self._guarda.registrar(clave)

    def obtener(self, ref: str) -> Credencial:
        _validar_ref(ref)
        entrada = self._leer()["entradas"].get(ref)
        if entrada is None:
            raise KeyError(ref)
        try:
            claro = self._aes.decrypt(
                base64.b64decode(entrada["n"]), base64.b64decode(entrada["c"]), _aad(ref)
            )
        except InvalidTag as e:
            raise BovedaError("llave incorrecta o bóveda alterada") from e
        return Credencial.model_validate_json(claro)

    def listar(self) -> list[str]:
        return sorted(self._leer()["entradas"])

    def eliminar(self, ref: str) -> None:
        datos = self._leer()
        if datos["entradas"].pop(_validar_ref(ref), None) is None:
            raise KeyError(ref)
        self._escribir(datos)

    # --- archivo ---------------------------------------------------------

    def _leer(self) -> dict:
        if not self._ruta.exists():
            return {"version": _VERSION, "entradas": {}}
        datos = json.loads(self._ruta.read_text())
        if datos.get("version") != _VERSION:
            raise BovedaError(f"versión de bóveda no soportada: {datos.get('version')}")
        return datos

    def _escribir(self, datos: dict) -> None:
        self._ruta.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporal = tempfile.mkstemp(dir=self._ruta.parent, prefix=".boveda-")
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w") as f:
                json.dump(datos, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporal, self._ruta)
        except BaseException:
            Path(temporal).unlink(missing_ok=True)
            raise
