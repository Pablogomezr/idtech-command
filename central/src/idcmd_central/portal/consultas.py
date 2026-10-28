"""Consultas del portal. Todas corren con `bd.como_usuario(...)`: la RLS decide qué sitios
ve cada quien; aquí no se filtra por permisos a propósito (un filtro olvidado no debe poder
abrir datos de otro cliente)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from psycopg import AsyncConnection
from psycopg.rows import dict_row

# Gravedad para resumir un equipo con varios chequeos: manda el peor.
GRAVEDAD = {"caido": 3, "degradado": 2, "desconocido": 1, "ok": 0}


@dataclass
class Chequeo:
    chequeo: str
    estado: str | None
    desde: datetime | None
    codigo: str | None
    metricas: dict
    observado_at: datetime | None


@dataclass
class Equipo:
    id: uuid.UUID
    nombre: str
    tipo_equipo: str
    marca: str | None
    modelo: str | None
    ubicacion: str | None
    chequeos: list[Chequeo] = field(default_factory=list)

    @property
    def estado(self) -> str:
        estados = [c.estado for c in self.chequeos if c.estado]
        return max(estados, key=GRAVEDAD.__getitem__) if estados else "sin_datos"


async def usuario(con: AsyncConnection) -> dict | None:
    cur = await con.cursor(row_factory=dict_row).execute(
        "SELECT id, nombre, correo, rol FROM idcmd.usuarios WHERE id = idcmd.usuario_actual()"
    )
    return await cur.fetchone()


async def sitios(con: AsyncConnection) -> list[dict]:
    cur = await con.cursor(row_factory=dict_row).execute(
        """
        WITH peor AS (
            SELECT d.sitio_id, d.id,
                   max(CASE e.estado WHEN 'caido' THEN 3 WHEN 'degradado' THEN 2
                                     WHEN 'desconocido' THEN 1 WHEN 'ok' THEN 0 END) AS nivel
              FROM idcmd.dispositivos d
              LEFT JOIN idcmd.estado_actual e ON e.dispositivo_id = d.id
             WHERE d.activo
             GROUP BY d.sitio_id, d.id
        )
        SELECT s.id, s.nombre, s.ciudad, c.nombre AS cliente,
               count(p.id) AS equipos,
               count(*) FILTER (WHERE p.nivel = 3) AS caidos,
               count(*) FILTER (WHERE p.nivel = 2) AS degradados,
               count(*) FILTER (WHERE p.nivel = 1 OR (p.id IS NOT NULL AND p.nivel IS NULL))
                   AS sin_dato,
               (SELECT max(a.ultimo_latido_at) FROM idcmd.agentes a
                 WHERE a.sitio_id = s.id AND a.revocado_at IS NULL) AS ultimo_latido
          FROM idcmd.sitios s
          JOIN idcmd.clientes c ON c.id = s.cliente_id
          LEFT JOIN peor p ON p.sitio_id = s.id
         GROUP BY s.id, c.nombre
         ORDER BY c.nombre, s.nombre
        """
    )
    return await cur.fetchall()


async def sitio(con: AsyncConnection, sitio_id: uuid.UUID) -> dict | None:
    cur = await con.cursor(row_factory=dict_row).execute(
        """
        SELECT s.id, s.nombre, s.ciudad, s.zona_horaria, c.nombre AS cliente,
               r.nombre AS regional,
               (SELECT max(a.ultimo_latido_at) FROM idcmd.agentes a
                 WHERE a.sitio_id = s.id AND a.revocado_at IS NULL) AS ultimo_latido
          FROM idcmd.sitios s
          JOIN idcmd.clientes c ON c.id = s.cliente_id
          JOIN idcmd.regionales r ON r.id = s.regional_id
         WHERE s.id = %s
        """,
        (sitio_id,),
    )
    return await cur.fetchone()


async def equipos(con: AsyncConnection, sitio_id: uuid.UUID) -> list[Equipo]:
    cur = await con.cursor(row_factory=dict_row).execute(
        """
        WITH ch AS (
            SELECT dispositivo_id, chequeo FROM idcmd.estado_actual WHERE sitio_id = %(s)s
            UNION
            SELECT dispositivo_id, chequeo FROM idcmd.metricas_actuales WHERE sitio_id = %(s)s
        )
        SELECT d.id, d.nombre, d.tipo_equipo::text AS tipo_equipo, d.marca, d.modelo,
               d.ubicacion, ch.chequeo,
               coalesce(e.estado, m.estado)::text AS estado, e.desde, e.codigo,
               coalesce(m.metricas, e.metricas, '{}') AS metricas, m.observado_at
          FROM idcmd.dispositivos d
          LEFT JOIN ch ON ch.dispositivo_id = d.id
          LEFT JOIN idcmd.estado_actual e
                 ON e.dispositivo_id = ch.dispositivo_id AND e.chequeo = ch.chequeo
          LEFT JOIN idcmd.metricas_actuales m
                 ON m.dispositivo_id = ch.dispositivo_id AND m.chequeo = ch.chequeo
         WHERE d.sitio_id = %(s)s AND d.activo
         ORDER BY d.nombre, ch.chequeo
        """,
        {"s": sitio_id},
    )
    por_id: dict[uuid.UUID, Equipo] = {}
    for f in await cur.fetchall():
        eq = por_id.setdefault(
            f["id"],
            Equipo(f["id"], f["nombre"], f["tipo_equipo"], f["marca"], f["modelo"], f["ubicacion"]),
        )
        if f["chequeo"]:
            eq.chequeos.append(
                Chequeo(
                    f["chequeo"], f["estado"], f["desde"], f["codigo"], f["metricas"] or {},
                    f["observado_at"],
                )
            )  # fmt: skip
    # Primero lo que está mal: es lo que el operador vino a ver.
    return sorted(por_id.values(), key=lambda e: (-GRAVEDAD.get(e.estado, 1), e.nombre))


async def operaciones(con: AsyncConnection, sitio_id: uuid.UUID, limite: int = 200) -> list[dict]:
    cur = await con.cursor(row_factory=dict_row).execute(
        """
        SELECT o.ocurrido_at, d.nombre AS equipo, o.tipo, o.cuenta,
               host(o.ip_origen) AS ip_origen, o.codigo_fabricante
          FROM idcmd.operaciones_equipo o
          JOIN idcmd.dispositivos d ON d.id = o.dispositivo_id
         WHERE o.sitio_id = %s
         ORDER BY o.ocurrido_at DESC
         LIMIT %s
        """,
        (sitio_id, limite),
    )
    return await cur.fetchall()


async def cambios(con: AsyncConnection, sitio_id: uuid.UUID, limite: int = 100) -> list[dict]:
    cur = await con.cursor(row_factory=dict_row).execute(
        """
        SELECT c.observado_at, d.nombre AS equipo, c.chequeo, c.antes, c.despues
          FROM idcmd.cambios_metricas c
          JOIN idcmd.dispositivos d ON d.id = c.dispositivo_id
         WHERE c.sitio_id = %s
         ORDER BY c.observado_at DESC
         LIMIT %s
        """,
        (sitio_id, limite),
    )
    return await cur.fetchall()


async def auditar(
    con: AsyncConnection,
    usuario_id: uuid.UUID | None,
    accion: str,
    recurso: str | None = None,
    ip: str | None = None,
    detalle: dict | None = None,
) -> None:
    from psycopg.types.json import Jsonb

    await con.execute(
        "INSERT INTO idcmd.auditoria_accesos (usuario_id, accion, recurso, ip, detalle)"
        " VALUES (%s, %s, %s, %s, %s)",
        (usuario_id, accion, recurso, ip, Jsonb(detalle or {})),
    )
