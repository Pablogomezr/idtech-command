"""Apariencia de los equipos: fotos por modelo (Administración > Apariencia) y su entrega.

La foto se reduce y se vuelve a codificar antes de guardarla: así pesa poco, no lleva metadatos
(EXIF con ubicación o con el nombre de quien la tomó) y lo que se sirve es siempre una imagen
generada aquí, no el archivo que subieron.
"""

from __future__ import annotations

import hashlib
import io
import warnings
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from PIL import Image, ImageOps, UnidentifiedImageError

from . import apariencia, consultas
from .admin import _admin, _filas, _volver
from .rutas import _a_ingreso, _ip, _mismo_origen, _pagina, _usuario_de_sesion

router = APIRouter()
MAX_SUBIDA = 8 * 1024 * 1024
LADO = 480  # suficiente para la tarjeta (92 px) en pantallas de alta densidad y para ampliar
MAX_PIXELES = 40_000_000  # una "bomba" de descompresión se rechaza antes de decodificarla


class FotoInvalida(ValueError):
    pass


def preparar_foto(contenido: bytes) -> tuple[str, bytes]:
    """(mime, imagen) lista para guardar: WebP de hasta 480 px, sin metadatos."""
    if len(contenido) > MAX_SUBIDA:
        raise FotoInvalida("la foto pesa más de 8 MB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            imagen = Image.open(io.BytesIO(contenido))
            if imagen.format not in ("JPEG", "PNG", "WEBP"):
                raise FotoInvalida("solo se aceptan fotos JPEG, PNG o WebP")
            if imagen.width * imagen.height > MAX_PIXELES:
                raise FotoInvalida("la foto es demasiado grande")
            imagen = ImageOps.exif_transpose(imagen)
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning,
            OSError) as e:  # fmt: skip
        raise FotoInvalida("el archivo no es una foto válida") from e
    imagen = imagen.convert("RGBA" if "A" in imagen.getbands() else "RGB")
    imagen.thumbnail((LADO, LADO))
    salida = io.BytesIO()
    imagen.save(salida, "WEBP", quality=85, method=4)
    return "image/webp", salida.getvalue()


@router.get("/portal/modelos/foto/{huella}")
async def foto(request: Request, huella: str):
    """La dirección lleva la huella del contenido: el navegador la guarda sin volver a pedirla."""
    usuario_id = await _usuario_de_sesion(request)
    if usuario_id is None:
        raise HTTPException(status_code=404)
    try:
        sha = bytes.fromhex(huella)
    except ValueError as e:
        raise HTTPException(status_code=404) from e
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        fila = await (
            await con.execute(
                "SELECT mime, contenido FROM idcmd.fotos_modelo WHERE sha256 = %s", (sha,)
            )
        ).fetchone()
    if fila is None:
        raise HTTPException(status_code=404)
    return Response(
        bytes(fila[1]),
        media_type=fila[0],
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


async def _referencias(con) -> list[dict]:
    """Cada referencia del inventario (o la que informa el propio equipo) con cuántos hay."""
    filas = await _filas(
        con,
        """
        SELECT d.tipo_equipo::text AS tipo, d.marca,
               coalesce(nullif(trim(d.modelo), ''),
                        (SELECT m.metricas->>'modelo' FROM idcmd.metricas_actuales m
                          WHERE m.dispositivo_id = d.id AND m.metricas ? 'modelo' LIMIT 1))
                 AS referencia
          FROM idcmd.dispositivos d WHERE d.activo
        """,
    )
    fotos = {
        f["modelo"]: f
        for f in await _filas(
            con,
            "SELECT modelo, encode(sha256, 'hex') AS huella, length(contenido) AS bytes,"
            " subido_at FROM idcmd.fotos_modelo",
        )
    }
    grupos: dict[tuple, dict] = {}
    for f in filas:
        clave = apariencia.clave_modelo(f["referencia"])
        g = grupos.setdefault(
            (clave, f["tipo"]),
            {"modelo": clave, "tipo": f["tipo"], "marca": f["marca"], "equipos": 0,
             "forma": apariencia.forma(f["tipo"], f["referencia"]),
             "foto": fotos.get(clave) if clave else None},
        )  # fmt: skip
        g["equipos"] += 1
    return sorted(
        grupos.values(), key=lambda g: (g["modelo"] is None, g["tipo"], g["modelo"] or "")
    )


@router.get("/portal/admin/apariencia", response_class=HTMLResponse)
async def ver_apariencia(request: Request, aviso: str | None = None):
    usuario_id, usuario = await _admin(request)
    if usuario_id is None:
        return _a_ingreso()
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        referencias = await _referencias(con)
    return _pagina(request, "admin_apariencia.html", {
        "usuario": usuario, "referencias": referencias, "aviso": aviso})  # fmt: skip


@router.post("/portal/admin/apariencia/foto")
async def subir_foto(
    request: Request,
    modelo: Annotated[str, Form(max_length=120)],
    archivo: Annotated[UploadFile, File()],
):
    _mismo_origen(request)
    usuario_id, _ = await _admin(request)
    if usuario_id is None:
        return _a_ingreso()
    clave = apariencia.clave_modelo(modelo)
    if clave is None:
        return _volver("/portal/admin/apariencia", "Falta la referencia del modelo.")
    try:
        mime, imagen = preparar_foto(await archivo.read(MAX_SUBIDA + 1))
    except FotoInvalida as e:
        return _volver("/portal/admin/apariencia", f"{clave}: {e}.")
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        await con.execute(
            "INSERT INTO idcmd.fotos_modelo (modelo, mime, contenido, sha256, subido_por)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (modelo) DO UPDATE SET mime = EXCLUDED.mime,"
            " contenido = EXCLUDED.contenido, sha256 = EXCLUDED.sha256,"
            " subido_por = EXCLUDED.subido_por, subido_at = now()",
            (clave, mime, imagen, hashlib.sha256(imagen).digest(), usuario_id),
        )
        await consultas.auditar(con, usuario_id, "admin_foto_modelo", "/portal/admin/apariencia",
                                _ip(request), {"modelo": clave, "accion": "subir"})  # fmt: skip
    return _volver("/portal/admin/apariencia", f"{clave}: foto actualizada.")


@router.post("/portal/admin/apariencia/quitar")
async def quitar_foto(request: Request, modelo: Annotated[str, Form(max_length=120)]):
    _mismo_origen(request)
    usuario_id, _ = await _admin(request)
    if usuario_id is None:
        return _a_ingreso()
    clave = apariencia.clave_modelo(modelo)
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        await con.execute("DELETE FROM idcmd.fotos_modelo WHERE modelo = %s", (clave,))
        await consultas.auditar(con, usuario_id, "admin_foto_modelo", "/portal/admin/apariencia",
                                _ip(request), {"modelo": clave, "accion": "quitar"})  # fmt: skip
    return _volver("/portal/admin/apariencia", f"{clave}: vuelve a la ilustración.")
