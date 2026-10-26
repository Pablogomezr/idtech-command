"""POST /api/v1/ingesta — única puerta de entrada de los agentes.

Respuestas que el agente sabe interpretar:
    200 lote registrado (idempotente)       401 token inválido o revocado
    409 el lote es de otro agente           413 cuerpo demasiado grande
    415 codificación no soportada           422 lote inválido
"""

from __future__ import annotations

import zlib

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from ..contrato import LoteIngesta
from ..db import AgenteAutenticado
from ..tokens import hash_token
from .servicio import ingerir

router = APIRouter()

MAX_COMPRIMIDO = 5 * 1024 * 1024
MAX_DESCOMPRIMIDO = 50 * 1024 * 1024  # protege contra "bombas" gzip


async def _autenticar(request: Request) -> AgenteAutenticado:
    cabecera = request.headers.get("authorization", "")
    tipo, _, token = cabecera.partition(" ")
    if tipo.lower() != "bearer" or not token:
        raise HTTPException(401, "token requerido")
    agente = await request.app.state.bd.autenticar_agente(hash_token(token.strip()))
    if agente is None:
        raise HTTPException(401, "token inválido o revocado")
    return agente


async def _leer_cuerpo(request: Request) -> bytes:
    partes, total = [], 0
    async for parte in request.stream():
        total += len(parte)
        if total > MAX_COMPRIMIDO:
            raise HTTPException(413, "cuerpo demasiado grande")
        partes.append(parte)
    cuerpo = b"".join(partes)

    codificacion = request.headers.get("content-encoding", "identity").lower()
    if codificacion == "identity":
        return cuerpo
    if codificacion != "gzip":
        raise HTTPException(415, "solo se acepta gzip")
    descompresor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        datos = descompresor.decompress(cuerpo, MAX_DESCOMPRIMIDO)
    except zlib.error as e:
        raise HTTPException(422, "gzip inválido") from e
    if descompresor.unconsumed_tail:
        raise HTTPException(413, "contenido descomprimido demasiado grande")
    return datos


@router.post("/api/v1/ingesta")
async def recibir_lote(request: Request) -> dict:
    agente = await _autenticar(request)
    cuerpo = await _leer_cuerpo(request)
    try:
        lote = LoteIngesta.model_validate_json(cuerpo)
    except ValidationError as e:
        # No se devuelve el contenido recibido: solo dónde está el error.
        campos = sorted({".".join(str(p) for p in err["loc"]) for err in e.errors()})[:10]
        raise HTTPException(422, {"error": "lote inválido", "campos": campos}) from None
    if lote.agente_id != agente.agente_id:
        raise HTTPException(409, "el lote declara otro agente_id")

    resultado = await ingerir(request.app.state.bd, agente, lote)
    return {
        "aceptados": resultado.aceptados,
        "rechazados": resultado.rechazados,
        "comandos": [],  # reservado para Fase 2 (reinicio remoto firmado)
    }
