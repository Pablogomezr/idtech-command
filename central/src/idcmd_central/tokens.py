"""Tokens de agente: aleatorios de 256 bits; en la base solo se guarda su SHA-256."""

from __future__ import annotations

import hashlib
import secrets

PREFIJO = "idcmd_"  # facilita que los escáneres de secretos lo detecten si se filtra


def generar_token() -> str:
    return PREFIJO + secrets.token_urlsafe(32)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()
