"""Buzón de salida en SQLite: todo se escribe aquí ANTES de intentar enviarlo.

Si el sitio pierde internet, los datos esperan en disco y se reenvían al
reconectar. Garantías:

* Durabilidad: WAL con `synchronous=FULL`; un corte de energía no pierde
  eventos ya confirmados.
* Atomicidad: el evento y el nuevo estado confirmado se guardan en la misma
  transacción, así un reinicio nunca deja el estado y el historial desalineados.
* Idempotencia: cada ítem tiene un UUID; encolarlo dos veces no lo duplica, y
  la central también descarta repetidos.
* Los ítems enviados se conservan `RETENCION_ENVIADOS` para poder reenviarlos a
  pedido (p. ej. si la central restaura un respaldo).
* Si el buzón se llena, se descartan primero los agregados más antiguos;
  los eventos nunca se descartan.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .modelos import AgregadoHora, Estado, EventoEstado, Instantanea, OperacionEquipo
from .seguridad import GuardaSecretos

RETENCION_ENVIADOS = timedelta(days=7)
MAX_PENDIENTES = 200_000

PRIORIDAD_EVENTO = 0
PRIORIDAD_AGREGADO = 1

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS salida (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    id         TEXT NOT NULL UNIQUE,
    tipo       TEXT NOT NULL CHECK (tipo IN ('evento', 'agregado', 'instantanea', 'operacion')),
    prioridad  INTEGER NOT NULL,
    cuerpo     TEXT NOT NULL,
    creado_at  TEXT NOT NULL,
    enviado_at TEXT
);
CREATE INDEX IF NOT EXISTS salida_pendientes ON salida (prioridad, seq) WHERE enviado_at IS NULL;
CREATE TABLE IF NOT EXISTS estados (
    dispositivo_id TEXT NOT NULL,
    chequeo        TEXT NOT NULL,
    estado         TEXT NOT NULL,
    desde          TEXT NOT NULL,
    PRIMARY KEY (dispositivo_id, chequeo)
);
"""


def _iso(momento: datetime) -> str:
    """Formato fijo en UTC: las comparaciones de texto en SQLite equivalen a las de fecha."""
    return momento.astimezone(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class ItemBuzon:
    id: str
    tipo: str
    cuerpo: str


class Buzon:
    def __init__(self, ruta: Path | str, guarda: GuardaSecretos | None = None):
        self._con = sqlite3.connect(ruta, isolation_level=None, check_same_thread=False)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=FULL")
        self._con.executescript(_ESQUEMA)
        self._guarda = guarda or GuardaSecretos()

    def cerrar(self) -> None:
        self._con.close()

    # --- escritura -------------------------------------------------------

    def registrar_transicion(self, evento: EventoEstado, momento: datetime) -> None:
        cuerpo = evento.model_dump_json()
        self._guarda.verificar(cuerpo)
        with self._transaccion():
            self._insertar(str(evento.id), "evento", PRIORIDAD_EVENTO, cuerpo, momento)
            self._con.execute(
                "INSERT INTO estados (dispositivo_id, chequeo, estado, desde) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (dispositivo_id, chequeo)"
                " DO UPDATE SET estado = excluded.estado, desde = excluded.desde",
                (
                    str(evento.dispositivo_id),
                    evento.chequeo,
                    evento.estado_nuevo.value,
                    _iso(evento.observado_at),
                ),
            )

    def encolar_agregado(self, agregado: AgregadoHora, momento: datetime) -> None:
        cuerpo = agregado.model_dump_json()
        self._guarda.verificar(cuerpo)
        with self._transaccion():
            self._insertar(str(agregado.id), "agregado", PRIORIDAD_AGREGADO, cuerpo, momento)

    def encolar_instantanea(self, instantanea: Instantanea, momento: datetime) -> None:
        cuerpo = instantanea.model_dump_json()
        self._guarda.verificar(cuerpo)
        with self._transaccion():
            self._insertar(str(instantanea.id), "instantanea", PRIORIDAD_AGREGADO, cuerpo, momento)

    def encolar_operacion(self, operacion: OperacionEquipo, momento: datetime) -> None:
        # Las operaciones son evidencia de auditoría: misma prioridad que los eventos y
        # nunca se descartan por falta de espacio.
        cuerpo = operacion.model_dump_json()
        self._guarda.verificar(cuerpo)
        with self._transaccion():
            self._insertar(str(operacion.id), "operacion", PRIORIDAD_EVENTO, cuerpo, momento)

    def _insertar(
        self, id_: str, tipo: str, prioridad: int, cuerpo: str, momento: datetime
    ) -> None:
        self._con.execute(
            "INSERT OR IGNORE INTO salida (id, tipo, prioridad, cuerpo, creado_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (id_, tipo, prioridad, cuerpo, _iso(momento)),
        )

    # --- lectura y envío -------------------------------------------------

    def tomar_lote(self, limite: int) -> list[ItemBuzon]:
        filas = self._con.execute(
            "SELECT id, tipo, cuerpo FROM salida WHERE enviado_at IS NULL"
            " ORDER BY prioridad, seq LIMIT ?",
            (limite,),
        ).fetchall()
        return [ItemBuzon(*f) for f in filas]

    def marcar_enviados(self, ids: list[str], momento: datetime) -> None:
        with self._transaccion():
            self._con.executemany(
                "UPDATE salida SET enviado_at = ? WHERE id = ?",
                [(_iso(momento), i) for i in ids],
            )

    def pendientes(self) -> int:
        return self._con.execute("SELECT count(*) FROM salida WHERE enviado_at IS NULL").fetchone()[
            0
        ]

    def estados(self) -> list[tuple[uuid.UUID, str, Estado, datetime]]:
        filas = self._con.execute("SELECT dispositivo_id, chequeo, estado, desde FROM estados")
        return [(uuid.UUID(d), c, Estado(e), datetime.fromisoformat(s)) for d, c, e, s in filas]

    # --- mantenimiento ---------------------------------------------------

    def purgar(self, ahora: datetime, max_pendientes: int = MAX_PENDIENTES) -> int:
        """Borra enviados vencidos y, si hay exceso, agregados pendientes antiguos."""
        with self._transaccion():
            borrados = self._con.execute(
                "DELETE FROM salida WHERE enviado_at IS NOT NULL AND enviado_at < ?",
                (_iso(ahora - RETENCION_ENVIADOS),),
            ).rowcount
            exceso = self.pendientes() - max_pendientes
            if exceso > 0:
                borrados += self._con.execute(
                    "DELETE FROM salida WHERE seq IN ("
                    " SELECT seq FROM salida WHERE enviado_at IS NULL"
                    " AND tipo IN ('agregado', 'instantanea')"
                    " ORDER BY seq LIMIT ?)",
                    (exceso,),
                ).rowcount
        return borrados

    def reenviar_desde(self, desde: datetime) -> int:
        """Vuelve a marcar como pendientes los ítems creados desde `desde`."""
        with self._transaccion():
            return self._con.execute(
                "UPDATE salida SET enviado_at = NULL"
                " WHERE enviado_at IS NOT NULL AND creado_at >= ?",
                (_iso(desde),),
            ).rowcount

    def _transaccion(self):
        return _Transaccion(self._con)


class _Transaccion:
    def __init__(self, con: sqlite3.Connection):
        self._con = con

    def __enter__(self):
        self._con.execute("BEGIN IMMEDIATE")

    def __exit__(self, tipo, *_):
        self._con.execute("ROLLBACK" if tipo else "COMMIT")
        return False
