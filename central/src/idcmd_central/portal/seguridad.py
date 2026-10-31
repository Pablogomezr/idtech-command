"""Segundo factor (TOTP), cifrado de su secreto y tokens de sesión del portal.

TOTP según RFC 6238 (SHA-1, 6 dígitos, 30 s): lo que entienden Google Authenticator,
Microsoft Authenticator y similares. Implementado con la biblioteca estándar para no sumar
una dependencia por 20 líneas.

El secreto TOTP se guarda cifrado con AES-256-GCM; la llave vive fuera de la base
(variable IDCMD_LLAVE_TOTP, en base64). Quien robe un respaldo de la base no obtiene los
segundos factores.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from urllib.parse import quote

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PASO_S = 30
DIGITOS = 6
_AAD = b"idcmd-totp-v1"


# --- TOTP ------------------------------------------------------------------------------


def generar_secreto() -> str:
    """Secreto de 160 bits en base32, como lo piden las aplicaciones de autenticación."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _clave_b32(secreto: str) -> bytes:
    relleno = "=" * (-len(secreto) % 8)
    return base64.b32decode(secreto.upper() + relleno)


def codigo_totp(secreto: str, momento: float | None = None) -> str:
    contador = int((time.time() if momento is None else momento) // PASO_S)
    resumen = hmac.new(_clave_b32(secreto), struct.pack(">Q", contador), hashlib.sha1).digest()
    desplazamiento = resumen[-1] & 0x0F
    numero = struct.unpack(">I", resumen[desplazamiento : desplazamiento + 4])[0] & 0x7FFFFFFF
    return str(numero % 10**DIGITOS).zfill(DIGITOS)


def verificar_totp(secreto: str, codigo: str, momento: float | None = None) -> bool:
    """Acepta el paso actual y el anterior/siguiente: relojes de celular desfasados ~30 s."""
    codigo = codigo.strip().replace(" ", "")
    if len(codigo) != DIGITOS or not codigo.isdigit():
        return False
    ahora = time.time() if momento is None else momento
    return any(
        hmac.compare_digest(codigo_totp(secreto, ahora + d * PASO_S), codigo) for d in (-1, 0, 1)
    )


def uri_totp(secreto: str, correo: str, emisor: str = "IDtech Command") -> str:
    """otpauth:// para escanear (o el secreto se escribe a mano en la aplicación)."""
    etiqueta = quote(f"{emisor}:{correo}")
    return f"otpauth://totp/{etiqueta}?secret={secreto}&issuer={quote(emisor)}&digits=6&period=30"


# --- Cifrado del secreto TOTP ----------------------------------------------------------


def generar_llave() -> str:
    return base64.b64encode(AESGCM.generate_key(bit_length=256)).decode()


def llave_desde_entorno() -> bytes:
    try:
        llave = base64.b64decode(os.environ["IDCMD_LLAVE_TOTP"])
    except KeyError as e:
        raise RuntimeError("defina IDCMD_LLAVE_TOTP (idcmd-central llave-totp)") from e
    if len(llave) != 32:
        raise RuntimeError("IDCMD_LLAVE_TOTP debe ser de 32 bytes en base64")
    return llave


def cifrar_secreto(secreto: str, llave: bytes) -> bytes:
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(llave).encrypt(nonce, secreto.encode(), _AAD)


def descifrar_secreto(cifrado: bytes, llave: bytes) -> str:
    return AESGCM(llave).decrypt(cifrado[:12], cifrado[12:], _AAD).decode()


# --- Sesiones ----------------------------------------------------------------------------


def generar_token_sesion() -> str:
    return secrets.token_urlsafe(32)


def hash_sesion(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


# --- Claves del portal generadas --------------------------------------------------------------

# Sin caracteres que se confunden (0/O, 1/l/I) para copiarla o dictarla sin errores.
_ALFABETO_CLAVE = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generar_clave() -> str:
    """Clave aleatoria de 4 grupos de 5 (~114 bits), p. ej. Kq7mX-p2t9V-dRhL4-w8Nc3."""
    return "-".join("".join(secrets.choice(_ALFABETO_CLAVE) for _ in range(5)) for _ in range(4))


def qr_svg(texto: str) -> str:
    """Código QR en SVG (para escanear el segundo factor con el celular)."""
    import io

    import segno

    salida = io.BytesIO()
    segno.make(texto, error="m").save(salida, kind="svg", scale=5, border=2, dark="#0c1726",
                                      xmldecl=False, svgns=True)  # fmt: skip
    return salida.getvalue().decode()
