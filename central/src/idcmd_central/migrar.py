"""Aplicador de migraciones SQL planas (`central/migraciones/NNNN_nombre.sql`).

Cada archivo se aplica una sola vez, en orden y dentro de una transacción. Se ejecuta con
el rol dueño del esquema, nunca con los roles de la aplicación.
"""

from __future__ import annotations

from pathlib import Path

import psycopg

DIRECTORIO = Path(__file__).resolve().parents[2] / "migraciones"


def pendientes(con: psycopg.Connection, directorio: Path = DIRECTORIO) -> list[Path]:
    con.execute(
        "CREATE TABLE IF NOT EXISTS public.schema_migraciones ("
        " version text PRIMARY KEY, aplicada_at timestamptz NOT NULL DEFAULT now())"
    )
    aplicadas = {f for (f,) in con.execute("SELECT version FROM public.schema_migraciones")}
    return [p for p in sorted(directorio.glob("*.sql")) if p.stem not in aplicadas]


def migrar(dsn: str, directorio: Path = DIRECTORIO) -> list[str]:
    aplicadas: list[str] = []
    with psycopg.connect(dsn, autocommit=True) as con:
        for archivo in pendientes(con, directorio):
            with con.transaction():
                con.execute(archivo.read_text(encoding="utf-8"))
                con.execute(
                    "INSERT INTO public.schema_migraciones (version) VALUES (%s)", (archivo.stem,)
                )
            aplicadas.append(archivo.stem)
    return aplicadas
