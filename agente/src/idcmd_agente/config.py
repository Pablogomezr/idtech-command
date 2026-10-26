"""Configuración del agente (archivo YAML). Nunca contiene secretos.

El token del agente y la llave de la bóveda se leen de archivos aparte
(Docker secrets). Las credenciales de los equipos viven solo en la bóveda.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .nucleo.modelos import MARGEN_ENVIO_PORTAL_S, PRESUPUESTO_DETECCION_S, Objetivo


class _Estricto(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfigCentral(_Estricto):
    url: str
    archivo_token: Path
    ca_adicional: Path | None = None


class ConfigDeteccion(_Estricto):
    confirmaciones_falla: int = Field(3, ge=1, le=10)
    confirmaciones_recuperacion: int = Field(2, ge=1, le=10)


class ConfigAlmacenamiento(_Estricto):
    buzon: Path = Path("/datos/buzon.sqlite3")
    boveda: Path = Path("/datos/boveda.json")
    archivo_llave: Path = Path("/run/secrets/idcmd_llave")


class ConfigAgente(_Estricto):
    agente_id: uuid.UUID
    central: ConfigCentral
    deteccion: ConfigDeteccion = Field(default_factory=ConfigDeteccion)
    almacenamiento: ConfigAlmacenamiento = Field(default_factory=ConfigAlmacenamiento)
    objetivos: list[Objetivo] = Field(default_factory=list)

    @model_validator(mode="after")
    def validar(self) -> ConfigAgente:
        vistos: set[tuple[uuid.UUID, str]] = set()
        for o in self.objetivos:
            clave = (o.dispositivo_id, o.adaptador)
            if clave in vistos:
                raise ValueError(f"objetivo duplicado: {o.dispositivo_id} con {o.adaptador}")
            vistos.add(clave)
            # Peor caso: la falla empieza justo después de un chequeo exitoso.
            peor_caso = o.intervalo_s * self.deteccion.confirmaciones_falla + MARGEN_ENVIO_PORTAL_S
            if peor_caso > PRESUPUESTO_DETECCION_S:
                raise ValueError(
                    f"{o.dispositivo_id}: intervalo {o.intervalo_s}s x "
                    f"{self.deteccion.confirmaciones_falla} confirmaciones + margen "
                    f"= {peor_caso}s; supera la detección comprometida de "
                    f"{PRESUPUESTO_DETECCION_S}s"
                )
        return self

    def leer_token(self) -> str:
        token = self.central.archivo_token.read_text().strip()
        if not token:
            raise ValueError(f"el archivo de token {self.central.archivo_token} está vacío")
        return token


def cargar_config(ruta: Path | str) -> ConfigAgente:
    with open(ruta, encoding="utf-8") as f:
        return ConfigAgente.model_validate(yaml.safe_load(f))
