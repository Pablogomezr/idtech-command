"""Administración desde el portal (solo admin): clientes, regionales y sitios; usuarios
(técnicos, clientes, administradores) con su acceso; asignación de técnicos a los sitios.

La RLS ya limita la escritura al administrador (políticas admin_escribe); aquí además se exige
el rol y se deja cada cambio en la auditoría. Las claves se generan al azar y se muestran una
sola vez con el QR del segundo factor; su hash se calcula dentro de Postgres.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import psycopg
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from psycopg.rows import dict_row

from . import consultas
from .rutas import _a_ingreso, _ip, _mismo_origen, _pagina, _usuario_de_sesion
from .seguridad import (
    cifrar_secreto,
    generar_clave,
    generar_secreto,
    llave_desde_entorno,
    qr_svg,
    uri_totp,
)

router = APIRouter()
ROLES = {"tecnico": "Técnico", "cliente": "Cliente (solo lectura)", "admin": "Administrador"}


async def _admin(request: Request):
    """(usuario_id, usuario) del administrador; 404 para los demás (no revela que existe)."""
    usuario_id = await _usuario_de_sesion(request)
    if usuario_id is None:
        return None, None
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        usuario = await consultas.usuario(con)
    if not usuario or usuario["rol"] != "admin":
        raise HTTPException(status_code=404)
    return usuario_id, usuario


def _volver(url: str, aviso: str | None = None) -> RedirectResponse:
    from urllib.parse import quote

    return RedirectResponse(url + (f"?aviso={quote(aviso)}" if aviso else ""), status_code=303)


async def _filas(con, sql: str, params=()) -> list[dict]:
    cur = await con.cursor(row_factory=dict_row).execute(sql, params)
    return await cur.fetchall()


async def _uno(con, sql: str, params=()):
    """Primera columna de la primera fila, o None."""
    fila = await (await con.execute(sql, params)).fetchone()
    return fila[0] if fila else None


def _texto(v: str | None, maximo: int = 120) -> str | None:
    v = (v or "").strip()
    return v[:maximo] or None


# --- Sitios ------------------------------------------------------------------------------------


async def _sitios(con) -> list[dict]:
    return await _filas(
        con,
        """
        SELECT s.id, s.nombre, s.ciudad, c.nombre AS cliente, r.nombre AS regional,
               (SELECT count(*) FROM idcmd.dispositivos d WHERE d.sitio_id = s.id AND d.activo)
                 AS equipos,
               (SELECT u.nombre FROM idcmd.asignaciones_tecnico a JOIN idcmd.usuarios u
                  ON u.id = a.tecnico_id WHERE a.sitio_id = s.id AND a.tipo = 'principal'
                  AND a.vigente_hasta IS NULL LIMIT 1) AS principal,
               (SELECT u.nombre FROM idcmd.asignaciones_tecnico a JOIN idcmd.usuarios u
                  ON u.id = a.tecnico_id WHERE a.sitio_id = s.id AND a.tipo = 'suplente'
                  AND a.vigente_hasta IS NULL LIMIT 1) AS suplente
          FROM idcmd.sitios s JOIN idcmd.clientes c ON c.id = s.cliente_id
          JOIN idcmd.regionales r ON r.id = s.regional_id
         ORDER BY c.nombre, s.nombre
        """,
    )


async def _tecnicos(con) -> list[dict]:
    return await _filas(
        con,
        "SELECT id, nombre, correo FROM idcmd.usuarios WHERE rol = 'tecnico' AND activo"
        " ORDER BY nombre",
    )


@router.get("/portal/admin", response_class=HTMLResponse)
async def inicio(request: Request, aviso: str | None = None):
    usuario_id, usuario = await _admin(request)
    if usuario_id is None:
        return _a_ingreso()
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        sitios = await _sitios(con)
        usuarios = await _usuarios(con)
    contexto = {
        "usuario": usuario,
        "sitios": sitios,
        "usuarios": usuarios,
        "aviso": aviso,
        "ROLES": ROLES,
    }
    return _pagina(request, "admin.html", contexto)


async def _form_sitio(request: Request, usuario, con, sitio: dict | None, error: str | None):
    clientes = await _filas(con, "SELECT id, nombre FROM idcmd.clientes ORDER BY nombre")
    regionales = await _filas(con, "SELECT DISTINCT nombre FROM idcmd.regionales ORDER BY nombre")
    return _pagina(
        request,
        "admin_sitio.html",
        {
            "usuario": usuario,
            "sitio": sitio,
            "clientes": clientes,
            "regionales": regionales,
            "tecnicos": await _tecnicos(con),
            "error": error,
        },
        estado=400 if error else 200,
    )


@router.get("/portal/admin/sitios/nuevo", response_class=HTMLResponse)
async def sitio_nuevo(request: Request):
    usuario_id, usuario = await _admin(request)
    if usuario_id is None:
        return _a_ingreso()
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        return await _form_sitio(request, usuario, con, None, None)


@router.get("/portal/admin/sitios/{sitio_id}", response_class=HTMLResponse)
async def sitio_editar(request: Request, sitio_id: uuid.UUID):
    usuario_id, usuario = await _admin(request)
    if usuario_id is None:
        return _a_ingreso()
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        filas = await _filas(
            con,
            "SELECT s.*, c.nombre AS cliente, r.nombre AS regional,"
            " (SELECT tecnico_id FROM idcmd.asignaciones_tecnico WHERE sitio_id = s.id AND"
            "  tipo = 'principal' AND vigente_hasta IS NULL LIMIT 1) AS principal_id,"
            " (SELECT tecnico_id FROM idcmd.asignaciones_tecnico WHERE sitio_id = s.id AND"
            "  tipo = 'suplente' AND vigente_hasta IS NULL LIMIT 1) AS suplente_id"
            " FROM idcmd.sitios s JOIN idcmd.clientes c ON c.id = s.cliente_id"
            " JOIN idcmd.regionales r ON r.id = s.regional_id WHERE s.id = %s",
            (sitio_id,),
        )
        if not filas:
            raise HTTPException(status_code=404)
        return await _form_sitio(request, usuario, con, filas[0], None)


async def _asignar(con, sitio_id, cliente_id, tipo: str, tecnico_id: uuid.UUID | None, quien):
    """Deja un solo técnico vigente de ese tipo en el sitio (o ninguno)."""
    actual = await (
        await con.execute(
            "SELECT tecnico_id FROM idcmd.asignaciones_tecnico WHERE sitio_id = %s AND tipo = %s"
            " AND vigente_hasta IS NULL",
            (sitio_id, tipo),
        )
    ).fetchone()
    if (actual[0] if actual else None) == tecnico_id:
        return
    await con.execute(
        "UPDATE idcmd.asignaciones_tecnico SET vigente_hasta = now() WHERE sitio_id = %s"
        " AND tipo = %s AND vigente_hasta IS NULL",
        (sitio_id, tipo),
    )
    if tecnico_id:
        await con.execute(
            "INSERT INTO idcmd.asignaciones_tecnico (tecnico_id, cliente_id, sitio_id, tipo,"
            " asignado_por) VALUES (%s, %s, %s, %s, %s)",
            (tecnico_id, cliente_id, sitio_id, tipo, quien),
        )


@router.post("/portal/admin/sitios")
async def sitio_guardar(
    request: Request,
    nombre: Annotated[str, Form(max_length=120)],
    cliente_id: Annotated[str, Form()] = "",
    cliente_nuevo: Annotated[str, Form(max_length=120)] = "",
    regional: Annotated[str, Form(max_length=120)] = "",
    ciudad: Annotated[str, Form(max_length=80)] = "",
    direccion: Annotated[str, Form(max_length=200)] = "",
    lat: Annotated[str, Form(max_length=20)] = "",
    lon: Annotated[str, Form(max_length=20)] = "",
    radio: Annotated[str, Form(max_length=6)] = "200",
    principal: Annotated[str, Form()] = "",
    suplente: Annotated[str, Form()] = "",
    sitio_id: Annotated[str, Form()] = "",
):
    _mismo_origen(request)
    usuario_id, usuario = await _admin(request)
    if usuario_id is None:
        return _a_ingreso()

    def numero(v, minimo, maximo):
        try:
            x = float(v.replace(",", "."))
        except ValueError:
            return None
        return x if minimo <= x <= maximo else None

    error = None
    la, lo = (numero(lat, -90, 90), numero(lon, -180, 180)) if lat or lon else (None, None)
    if (lat or lon) and (la is None or lo is None):
        error = "Latitud y longitud deben ser números (p. ej. 10.9878 y -74.7889)."
    if not _texto(nombre):
        error = "El sitio necesita un nombre."
    if not (cliente_id or _texto(cliente_nuevo)):
        error = "Elija el cliente o escriba el nombre de uno nuevo."
    radio_m = int(numero(radio or "200", 20, 5000) or 200)
    tecnico = {
        t: (uuid.UUID(v) if v else None)
        for t, v in (("principal", principal), ("suplente", suplente))
    }
    if tecnico["principal"] and tecnico["principal"] == tecnico["suplente"]:
        error = "El técnico principal y el suplente deben ser personas distintas."
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        if error:
            sitio = {
                "id": sitio_id or None,
                "nombre": nombre,
                "ciudad": ciudad,
                "direccion": direccion,
                "lat": lat,
                "lon": lon,
                "radio_checkin_m": radio,
                "cliente_id": cliente_id,
                "regional": regional,
            }
            return await _form_sitio(request, usuario, con, sitio, error)
        if cliente_id:
            cliente = uuid.UUID(cliente_id)
        else:
            cliente = (
                await (
                    await con.execute(
                        "INSERT INTO idcmd.clientes (nombre) VALUES (%s) RETURNING id",
                        (_texto(cliente_nuevo),),
                    )
                ).fetchone()
            )[0]
        nombre_regional = _texto(regional) or "General"
        regional_id = await _uno(
            con,
            "SELECT id FROM idcmd.regionales WHERE cliente_id = %s AND lower(nombre) = lower(%s)",
            (cliente, nombre_regional),
        ) or await _uno(
            con,
            "INSERT INTO idcmd.regionales (cliente_id, nombre) VALUES (%s, %s) RETURNING id",
            (cliente, nombre_regional),
        )
        datos = (_texto(nombre), _texto(ciudad, 80), _texto(direccion, 200), la, lo, radio_m)
        if sitio_id:
            sid = uuid.UUID(sitio_id)
            # El cliente de un sitio no cambia: sus equipos, tickets e informes son de ese cliente.
            n = await con.execute(
                "UPDATE idcmd.sitios SET nombre = %s, ciudad = %s, direccion = %s, lat = %s,"
                " lon = %s, radio_checkin_m = %s, regional_id = %s WHERE id = %s"
                " AND cliente_id = %s",
                (*datos, regional_id, sid, cliente),
            )
            if n.rowcount != 1:
                raise HTTPException(status_code=400, detail="el cliente del sitio no se cambia")
        else:
            sid = await _uno(
                con,
                "INSERT INTO idcmd.sitios (nombre, ciudad, direccion, lat, lon, radio_checkin_m,"
                " regional_id, cliente_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                " RETURNING id",
                (*datos, regional_id, cliente),
            )
        for tipo, tecnico_id in tecnico.items():
            await _asignar(con, sid, cliente, tipo, tecnico_id, usuario_id)
        detalle = {"nombre": _texto(nombre), "nuevo": not sitio_id}
        await consultas.auditar(
            con, usuario_id, "admin_sitio", f"/portal/admin/sitios/{sid}", _ip(request), detalle
        )
    return _volver("/portal/admin", f"Sitio «{_texto(nombre)}» guardado.")


# --- Usuarios ----------------------------------------------------------------------------------


async def _usuarios(con) -> list[dict]:
    return await _filas(
        con,
        """
        SELECT u.id, u.nombre, u.correo, u.rol::text AS rol, u.activo,
               coalesce((SELECT string_agg(s.nombre || CASE a.tipo WHEN 'suplente'
                             THEN ' (suplente)' ELSE '' END, ', ' ORDER BY s.nombre)
                           FROM idcmd.asignaciones_tecnico a
                           JOIN idcmd.sitios s ON s.id = a.sitio_id
                          WHERE a.tecnico_id = u.id AND a.vigente_hasta IS NULL),
                        (SELECT string_agg(coalesce(s.nombre, r.nombre, c.nombre), ', ')
                           FROM idcmd.alcances_usuario x
                           JOIN idcmd.clientes c ON c.id = x.cliente_id
                           LEFT JOIN idcmd.regionales r ON r.id = x.regional_id
                           LEFT JOIN idcmd.sitios s ON s.id = x.sitio_id
                          WHERE x.usuario_id = u.id)) AS acceso
          FROM idcmd.usuarios u
         ORDER BY u.activo DESC, array_position(ARRAY['admin','tecnico','cliente'], u.rol::text),
                  u.nombre
        """,
    )


async def _form_usuario(request, usuario, con, previo: dict | None, error: str | None):
    return _pagina(
        request,
        "admin_usuario.html",
        {
            "usuario": usuario,
            "previo": previo or {},
            "error": error,
            "ROLES": ROLES,
            "sitios": await _sitios(con),
            "clientes": await _filas(con, "SELECT id, nombre FROM idcmd.clientes ORDER BY nombre"),
        },
        estado=400 if error else 200,
    )


@router.get("/portal/admin/usuarios/nuevo", response_class=HTMLResponse)
async def usuario_nuevo(request: Request):
    usuario_id, usuario = await _admin(request)
    if usuario_id is None:
        return _a_ingreso()
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        return await _form_usuario(request, usuario, con, None, None)


def _credenciales(request, usuario, correo: str, nombre: str, clave: str, secreto: str, motivo):
    uri = uri_totp(secreto, correo)
    return _pagina(
        request,
        "admin_credenciales.html",
        {
            "usuario": usuario,
            "correo": correo,
            "nombre": nombre,
            "clave": clave,
            "motivo": motivo,
            "secreto": " ".join(secreto[i : i + 4] for i in range(0, len(secreto), 4)),
            "qr": qr_svg(uri),
        },
    )


async def _dar_acceso(
    con,
    nuevo: uuid.UUID,
    rol: str,
    sitios: list[uuid.UUID],
    cliente: uuid.UUID | None,
    suplente: bool,
    quien,
) -> None:
    if rol == "tecnico":
        for sid in sitios:
            fila = await (
                await con.execute("SELECT cliente_id FROM idcmd.sitios WHERE id = %s", (sid,))
            ).fetchone()
            if fila:
                await _asignar(
                    con, sid, fila[0], "suplente" if suplente else "principal", nuevo, quien
                )
    elif rol == "cliente":
        if cliente and not sitios:
            await con.execute(
                "INSERT INTO idcmd.alcances_usuario (usuario_id, cliente_id) VALUES (%s, %s)",
                (nuevo, cliente),
            )
        for sid in sitios:
            await con.execute(
                "INSERT INTO idcmd.alcances_usuario (usuario_id, cliente_id, sitio_id)"
                " SELECT %s, cliente_id, id FROM idcmd.sitios WHERE id = %s",
                (nuevo, sid),
            )


@router.post("/portal/admin/usuarios", response_class=HTMLResponse)
async def usuario_crear(
    request: Request,
    nombre: Annotated[str, Form(max_length=120)],
    correo: Annotated[str, Form(max_length=254)],
    rol: Annotated[str, Form(pattern="^(tecnico|cliente|admin)$")],
    cliente_id: Annotated[str, Form()] = "",
    suplente: Annotated[str, Form()] = "",
):
    _mismo_origen(request)
    usuario_id, usuario = await _admin(request)
    if usuario_id is None:
        return _a_ingreso()
    formulario = await request.form()
    sitios = [uuid.UUID(v) for v in formulario.getlist("sitios") if v]
    cliente = uuid.UUID(cliente_id) if cliente_id else None
    correo = correo.strip().lower()
    previo = {
        "nombre": nombre,
        "correo": correo,
        "rol": rol,
        "sitios": [str(s) for s in sitios],
        "cliente_id": cliente_id,
        "suplente": bool(suplente),
    }
    error = None
    if "@" not in correo or "." not in correo.split("@")[-1]:
        error = "Escriba un correo válido."
    elif not _texto(nombre):
        error = "Escriba el nombre."
    elif rol == "tecnico" and not sitios:
        error = "Un técnico necesita al menos un sitio: si no, entraría y no vería nada."
    elif rol == "cliente" and not (cliente or sitios):
        error = "Un usuario cliente necesita su cliente (todos los sitios) o sitios puntuales."
    clave, secreto = generar_clave(), generar_secreto()
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        if not error:
            try:
                async with con.transaction():
                    nuevo = (
                        await (
                            await con.execute(
                                "SELECT idcmd.crear_usuario_portal(%s, %s, %s, %s, %s)",
                                (
                                    correo,
                                    _texto(nombre),
                                    rol,
                                    clave,
                                    cifrar_secreto(secreto, llave_desde_entorno()),
                                ),
                            )
                        ).fetchone()
                    )[0]
                    await _dar_acceso(con, nuevo, rol, sitios, cliente, bool(suplente), usuario_id)
            except psycopg.errors.UniqueViolation:
                error = f"Ya existe un usuario con el correo {correo}. Búsquelo en la lista."
        if error:
            return await _form_usuario(request, usuario, con, previo, error)
        await consultas.auditar(
            con,
            usuario_id,
            "admin_usuario_crear",
            "/portal/admin/usuarios",
            _ip(request),
            {"correo": correo, "rol": rol},
        )
    return _credenciales(request, usuario, correo, _texto(nombre), clave, secreto, "nuevo")


async def _otro(con, objetivo: uuid.UUID) -> dict:
    filas = await _filas(
        con,
        "SELECT id, nombre, correo, rol::text AS rol FROM idcmd.usuarios WHERE id = %s",
        (objetivo,),
    )
    if not filas:
        raise HTTPException(status_code=404)
    return filas[0]


@router.post("/portal/admin/usuarios/{objetivo}/restablecer", response_class=HTMLResponse)
async def usuario_restablecer(request: Request, objetivo: uuid.UUID):
    _mismo_origen(request)
    usuario_id, usuario = await _admin(request)
    if usuario_id is None:
        return _a_ingreso()
    clave, secreto = generar_clave(), generar_secreto()
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        otro = await _otro(con, objetivo)
        await con.execute(
            "SELECT idcmd.restablecer_acceso(%s, %s, %s)",
            (objetivo, clave, cifrar_secreto(secreto, llave_desde_entorno())),
        )
        await consultas.auditar(
            con,
            usuario_id,
            "admin_usuario_restablecer",
            f"/portal/admin/usuarios/{objetivo}",
            _ip(request),
            {"correo": otro["correo"]},
        )
    return _credenciales(
        request, usuario, otro["correo"], otro["nombre"], clave, secreto, "restablecido"
    )


@router.post("/portal/admin/usuarios/{objetivo}/activo")
async def usuario_activo(
    request: Request, objetivo: uuid.UUID, activo: Annotated[str, Form(pattern="^(si|no)$")]
):
    _mismo_origen(request)
    usuario_id, _ = await _admin(request)
    if usuario_id is None:
        return _a_ingreso()
    if objetivo == usuario_id and activo == "no":
        return _volver("/portal/admin", "No puede desactivar su propio usuario.")
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        otro = await _otro(con, objetivo)
        await con.execute("SELECT idcmd.cambiar_activo(%s, %s)", (objetivo, activo == "si"))
        await consultas.auditar(
            con,
            usuario_id,
            "admin_usuario_activo",
            f"/portal/admin/usuarios/{objetivo}",
            _ip(request),
            {"correo": otro["correo"], "activo": activo == "si"},
        )
    estado = "activado" if activo == "si" else "desactivado (sus sesiones se cerraron)"
    return _volver("/portal/admin", f"{otro['nombre']}: {estado}.")


@router.get("/portal/admin/usuarios/{objetivo}", response_class=HTMLResponse)
async def usuario_acceso(request: Request, objetivo: uuid.UUID, aviso: str | None = None):
    usuario_id, usuario = await _admin(request)
    if usuario_id is None:
        return _a_ingreso()
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        otro = await _otro(con, objetivo)
        asignaciones = await _filas(
            con,
            "SELECT a.id, s.nombre AS sitio, a.tipo::text AS tipo FROM idcmd.asignaciones_tecnico a"
            " JOIN idcmd.sitios s ON s.id = a.sitio_id WHERE a.tecnico_id = %s"
            " AND a.vigente_hasta IS NULL ORDER BY s.nombre",
            (objetivo,),
        )
        alcances = await _filas(
            con,
            "SELECT x.id, c.nombre AS cliente, s.nombre AS sitio FROM idcmd.alcances_usuario x"
            " JOIN idcmd.clientes c ON c.id = x.cliente_id LEFT JOIN idcmd.sitios s"
            " ON s.id = x.sitio_id WHERE x.usuario_id = %s ORDER BY c.nombre, s.nombre",
            (objetivo,),
        )
        return _pagina(
            request,
            "admin_acceso.html",
            {
                "usuario": usuario,
                "otro": otro,
                "asignaciones": asignaciones,
                "alcances": alcances,
                "sitios": await _sitios(con),
                "aviso": aviso,
                "ROLES": ROLES,
                "clientes": await _filas(
                    con, "SELECT id, nombre FROM idcmd.clientes ORDER BY nombre"
                ),
            },
        )


@router.post("/portal/admin/usuarios/{objetivo}/acceso")
async def usuario_acceso_guardar(request: Request, objetivo: uuid.UUID):
    _mismo_origen(request)
    usuario_id, _ = await _admin(request)
    if usuario_id is None:
        return _a_ingreso()
    formulario = await request.form()
    quitar = formulario.get("quitar")
    url = f"/portal/admin/usuarios/{objetivo}"
    async with request.app.state.bd.como_usuario(usuario_id) as con:
        otro = await _otro(con, objetivo)
        if quitar:
            tabla = "asignaciones_tecnico" if otro["rol"] == "tecnico" else "alcances_usuario"
            if tabla == "asignaciones_tecnico":
                await con.execute(
                    "UPDATE idcmd.asignaciones_tecnico SET vigente_hasta = now() WHERE id = %s"
                    " AND tecnico_id = %s AND vigente_hasta IS NULL",
                    (uuid.UUID(quitar), objetivo),
                )
            else:
                await con.execute(
                    "DELETE FROM idcmd.alcances_usuario WHERE id = %s AND usuario_id = %s",
                    (uuid.UUID(quitar), objetivo),
                )
            aviso = "Acceso quitado."
        else:
            sitios = [uuid.UUID(v) for v in formulario.getlist("sitios") if v]
            cliente = uuid.UUID(formulario["cliente_id"]) if formulario.get("cliente_id") else None
            await _dar_acceso(
                con,
                objetivo,
                otro["rol"],
                sitios,
                cliente,
                bool(formulario.get("suplente")),
                usuario_id,
            )
            aviso = "Acceso agregado."
        await consultas.auditar(
            con,
            usuario_id,
            "admin_usuario_acceso",
            url,
            _ip(request),
            {"correo": otro["correo"], "quitar": bool(quitar)},
        )
    return _volver(url, aviso)
