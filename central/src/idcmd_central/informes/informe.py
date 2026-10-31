"""Informe mensual de SLA de un sitio: junta los datos (con la RLS de quien lo pide) y arma un
resumen serializable. El mismo resumen se muestra en el portal, se vuelve PDF y se guarda como
informe emitido al cerrar el mes."""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from ..portal.presentacion import ZONA, nombre_codigo
from . import calculo

VERSION = 1
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre"]  # fmt: skip
# Los agregados de la hora recién cerrada tardan unos minutos en llegar desde el agente.
MARGEN_LLEGADA = timedelta(minutes=15)
MESES_TENDENCIA = 6


def periodo_de(texto: str) -> date:
    """'2026-10' -> date(2026, 10, 1). ValueError si no es un mes válido."""
    anio, mes = texto.split("-")
    if len(anio) != 4 or len(mes) != 2:
        raise ValueError(texto)
    return date(int(anio), int(mes), 1)


def texto_periodo(periodo: date) -> str:
    return f"{periodo:%Y-%m}"


def nombre_periodo(periodo: date) -> str:
    return f"{MESES[periodo.month - 1]} de {periodo.year}"


def mes_anterior(periodo: date, n: int = 1) -> date:
    total = periodo.year * 12 + periodo.month - 1 - n
    return date(total // 12, total % 12 + 1, 1)


def limites(periodo: date) -> tuple[datetime, datetime]:
    """El mes en hora de Bogotá, como instantes."""
    inicio = datetime(periodo.year, periodo.month, 1, tzinfo=ZONA)
    siguiente = mes_anterior(periodo, -1)
    return inicio, datetime(siguiente.year, siguiente.month, 1, tzinfo=ZONA)


def mes_cerrado(periodo: date, ahora: datetime) -> bool:
    return limites(periodo)[1] + MARGEN_LLEGADA <= ahora


# --- Datos -----------------------------------------------------------------------------------


async def _filas(con: AsyncConnection, sql: str, params) -> list[dict]:
    cur = await con.cursor(row_factory=dict_row).execute(sql, params)
    return await cur.fetchall()


async def _medir_equipos(
    con: AsyncConnection, sitio_id: uuid.UUID, desde: datetime, hasta: datetime
) -> tuple[list[dict], dict[uuid.UUID, calculo.Medicion], list[dict]]:
    equipos = await _filas(
        con,
        "SELECT id, nombre, tipo_equipo::text AS tipo, marca, modelo, criticidad,"
        " retencion_contratada_dias FROM idcmd.dispositivos"
        " WHERE sitio_id = %s AND activo ORDER BY nombre",
        (sitio_id,),
    )
    iniciales = await _filas(
        con,
        "SELECT DISTINCT ON (dispositivo_id, chequeo) dispositivo_id, chequeo,"
        " estado_nuevo::text AS estado FROM idcmd.eventos_estado"
        " WHERE sitio_id = %s AND observado_at < %s"
        " ORDER BY dispositivo_id, chequeo, observado_at DESC",
        (sitio_id, desde),
    )
    cambios = await _filas(
        con,
        "SELECT dispositivo_id, chequeo, observado_at, estado_nuevo::text AS estado"
        " FROM idcmd.eventos_estado WHERE sitio_id = %s AND observado_at >= %s"
        " AND observado_at < %s",
        (sitio_id, desde, hasta),
    )
    horas = await _filas(
        con,
        "SELECT DISTINCT dispositivo_id, chequeo, hora FROM idcmd.agregados_hora"
        " WHERE sitio_id = %s AND hora >= %s AND hora < %s",
        (sitio_id, calculo.truncar_hora(desde), hasta),
    )
    inicios = await _filas(
        con,
        "SELECT dispositivo_id, min(m) AS inicio FROM ("
        " SELECT dispositivo_id, min(observado_at) AS m FROM idcmd.eventos_estado"
        "  WHERE sitio_id = %s GROUP BY dispositivo_id"
        " UNION ALL SELECT dispositivo_id, min(hora) FROM idcmd.agregados_hora"
        "  WHERE sitio_id = %s GROUP BY dispositivo_id) x GROUP BY dispositivo_id",
        (sitio_id, sitio_id),
    )
    exclusiones = await _filas(
        con,
        "SELECT e.dispositivo_id, e.motivo, e.desde, e.hasta, e.descripcion,"
        " d.nombre AS equipo FROM idcmd.exclusiones_sla e"
        " LEFT JOIN idcmd.dispositivos d ON d.id = e.dispositivo_id"
        " WHERE e.sitio_id = %s AND e.desde < %s AND e.hasta > %s ORDER BY e.desde",
        (sitio_id, hasta, desde),
    )

    por_chequeo: dict[uuid.UUID, dict[str, dict]] = defaultdict(dict)
    for f in iniciales:
        por_chequeo[f["dispositivo_id"]].setdefault(f["chequeo"], {"inicial": None, "cambios": []})
        por_chequeo[f["dispositivo_id"]][f["chequeo"]]["inicial"] = f["estado"]
    for f in cambios:
        c = por_chequeo[f["dispositivo_id"]].setdefault(
            f["chequeo"], {"inicial": None, "cambios": []}
        )
        c["cambios"].append((f["observado_at"], f["estado"]))
    horas_de: dict[tuple[uuid.UUID, str], set[datetime]] = defaultdict(set)
    for f in horas:
        horas_de[(f["dispositivo_id"], f["chequeo"])].add(f["hora"])
        por_chequeo[f["dispositivo_id"]].setdefault(f["chequeo"], {"inicial": None, "cambios": []})
    inicio_de = {f["dispositivo_id"]: f["inicio"] for f in inicios}

    mediciones: dict[uuid.UUID, calculo.Medicion] = {}
    for e in equipos:
        inicio = inicio_de.get(e["id"])
        if inicio is None or inicio >= hasta:
            continue  # todavía no reportaba: no se mide
        # Desde la hora en que empezó a reportar (la primera hora tiene su agregado).
        desde_equipo = max(desde, calculo.truncar_hora(inicio))
        lineas = [
            calculo.aplicar_sin_datos(
                calculo.linea_de_tiempo(c["inicial"], c["cambios"], desde_equipo, hasta),
                calculo.horas_sin_datos(horas_de[(e["id"], chequeo)], desde_equipo, hasta),
            )
            for chequeo, c in por_chequeo[e["id"]].items()
        ]
        linea = calculo.combinar(lineas, desde_equipo, hasta)
        propias = [
            (max(x["desde"], desde_equipo), min(x["hasta"], hasta))
            for x in exclusiones
            if x["dispositivo_id"] in (None, e["id"])
        ]
        mediciones[e["id"]] = calculo.medir(linea, [], propias, desde_equipo, hasta)
    return equipos, mediciones, exclusiones


def _hasta(periodo: date, ahora: datetime) -> tuple[datetime, datetime, bool]:
    desde, fin = limites(periodo)
    hasta = min(fin, calculo.truncar_hora(ahora - MARGEN_LLEGADA))
    return desde, max(desde, hasta), hasta < fin


async def disponibilidad_sitio(
    con: AsyncConnection, sitio_id: uuid.UUID, periodo: date, ahora: datetime
) -> float | None:
    desde, hasta, _ = _hasta(periodo, ahora)
    equipos, mediciones, _ = await _medir_equipos(con, sitio_id, desde, hasta)
    return calculo.ponderada(
        (mediciones[e["id"]].disponibilidad, e["criticidad"])
        for e in equipos
        if e["id"] in mediciones
    )


async def construir(
    con: AsyncConnection, sitio_id: uuid.UUID, periodo: date, ahora: datetime
) -> dict | None:
    """Resumen del informe, o None si el sitio no existe o no es visible para quien pide."""
    sitio = await _filas(
        con,
        "SELECT s.nombre AS sitio, s.ciudad, c.nombre AS cliente, r.nombre AS regional,"
        " c.meta_disponibilidad::float AS meta FROM idcmd.sitios s"
        " JOIN idcmd.clientes c ON c.id = s.cliente_id"
        " LEFT JOIN idcmd.regionales r ON r.id = s.regional_id WHERE s.id = %s",
        (sitio_id,),
    )
    if not sitio:
        return None
    sitio = sitio[0]
    desde, hasta, parcial = _hasta(periodo, ahora)
    equipos, mediciones, exclusiones = await _medir_equipos(con, sitio_id, desde, hasta)

    filas_equipos, caidas = [], []
    totales = calculo.Medicion()
    for e in equipos:
        m = mediciones.get(e["id"])
        if m is None:
            filas_equipos.append({"nombre": e["nombre"], "tipo": e["tipo"], "marca": e["marca"],
                                  "criticidad": e["criticidad"], "medido": False})  # fmt: skip
            continue
        for campo in ("arriba_s", "degradado_s", "caido_s", "sin_datos_s", "excluido_s"):
            setattr(totales, campo, getattr(totales, campo) + getattr(m, campo))
        filas_equipos.append({
            "nombre": e["nombre"], "tipo": e["tipo"], "marca": e["marca"],
            "criticidad": e["criticidad"], "medido": True, "disponibilidad": m.disponibilidad,
            "caido_s": m.caido_s, "degradado_s": m.degradado_s, "sin_datos_s": m.sin_datos_s,
            "excluido_s": m.excluido_s, "caidas": len(m.caidas),
        })  # fmt: skip
        caidas += [
            {"equipo": e["nombre"], "inicio": a.isoformat(), "fin": b.isoformat(),
             "duracion_s": (b - a).total_seconds(), "en_curso": b >= hasta and parcial}
            for a, b in m.caidas
        ]  # fmt: skip
    filas_equipos.sort(key=lambda f: (not f["medido"], f.get("disponibilidad") or 0, f["nombre"]))
    caidas.sort(key=lambda c: -c["duracion_s"])
    disponibilidad = calculo.ponderada(
        (mediciones[e["id"]].disponibilidad, e["criticidad"])
        for e in equipos
        if e["id"] in mediciones
    )

    tickets = await _filas(
        con,
        "SELECT t.numero, t.titulo, t.codigo, t.prioridad::text AS prioridad,"
        " t.estado::text AS estado, t.cierre, t.abierto_at, t.aceptado_at, t.resuelto_at,"
        " t.vence_respuesta_at, t.vence_solucion_at, d.nombre AS equipo"
        " FROM idcmd.tickets t LEFT JOIN idcmd.dispositivos d ON d.id = t.dispositivo_id"
        " WHERE t.sitio_id = %s AND t.abierto_at >= %s AND t.abierto_at < %s"
        " ORDER BY t.abierto_at",
        (sitio_id, desde, limites(periodo)[1]),
    )
    ind = calculo.indicadores_tickets(tickets, ahora)
    orden = ["critica", "alta", "media", "baja"]
    por_prioridad = [
        {"prioridad": p, "total": v["total"], "no_aplica": v["no_aplica"],
         "respuesta_pct": v["respuesta"].porcentaje, "solucion_pct": v["solucion"].porcentaje,
         "pendientes": v["respuesta"].pendientes + v["solucion"].pendientes}
        for p, v in sorted(ind["por_prioridad"].items(), key=lambda kv: orden.index(kv[0]))
    ]  # fmt: skip

    def cumple(hecho, vence):
        if hecho is not None:
            return hecho <= vence
        return False if ahora > vence else None

    lista = [
        {"numero": t["numero"], "titulo": t["titulo"], "equipo": t["equipo"],
         "prioridad": t["prioridad"], "estado": t["estado"], "cierre": t["cierre"],
         "abierto_at": t["abierto_at"].isoformat(),
         "respuesta_ok": None if t["cierre"] in ("cancelado", "recuperado_solo")
         else cumple(t["aceptado_at"], t["vence_respuesta_at"]),
         "solucion_ok": None if t["cierre"] in ("cancelado", "recuperado_solo")
         else cumple(t["resuelto_at"], t["vence_solucion_at"])}
        for t in tickets
    ]  # fmt: skip
    fallas = Counter(t["codigo"] for t in tickets)

    retencion = await _retencion(con, sitio_id, equipos)

    tendencia = []
    for n in range(MESES_TENDENCIA - 1, 0, -1):
        p = mes_anterior(periodo, n)
        valor = await _disponibilidad_guardada_o_calculada(con, sitio_id, p, ahora)
        tendencia.append({"periodo": texto_periodo(p), "disponibilidad": valor})
    tendencia.append({"periodo": texto_periodo(periodo), "disponibilidad": disponibilidad})

    return {
        "version": VERSION,
        "periodo": texto_periodo(periodo),
        "periodo_texto": nombre_periodo(periodo),
        "desde": desde.isoformat(),
        "hasta": hasta.isoformat(),
        "parcial": parcial,
        "generado_at": ahora.isoformat(),
        "cliente": sitio["cliente"],
        "regional": sitio["regional"],
        "sitio": sitio["sitio"],
        "ciudad": sitio["ciudad"],
        "meta": sitio["meta"],
        "disponibilidad": disponibilidad,
        "cumple_meta": None if disponibilidad is None else disponibilidad >= sitio["meta"],
        "totales": {
            "equipos": len(equipos),
            "medidos": len(mediciones),
            "caido_s": totales.caido_s,
            "degradado_s": totales.degradado_s,
            "sin_datos_s": totales.sin_datos_s,
            "excluido_s": totales.excluido_s,
            "caidas": len(caidas),
        },
        "equipos": filas_equipos,
        "caidas": caidas[:10],
        "exclusiones": [
            {
                "motivo": x["motivo"],
                "equipo": x["equipo"],
                "descripcion": x["descripcion"],
                "desde": x["desde"].isoformat(),
                "hasta": x["hasta"].isoformat(),
            }
            for x in exclusiones
        ],  # fmt: skip
        "tickets": {
            "total": len(tickets),
            "por_prioridad": por_prioridad,
            "respuesta_pct": ind["respuesta"].porcentaje,
            "solucion_pct": ind["solucion"].porcentaje,
            "respuesta_prom_s": ind["respuesta_prom_s"],
            "mttr_s": ind["mttr_s"],
            "lista": lista,
        },
        "fallas_top": [
            {"codigo": c, "nombre": nombre_codigo(c), "veces": n} for c, n in fallas.most_common(5)
        ],
        "retencion": retencion,
        "tendencia": tendencia,
    }


async def _retencion(con: AsyncConnection, sitio_id: uuid.UUID, equipos: list[dict]) -> list[dict]:
    """Retención de video por cámara al momento de generar (la métrica no guarda historial)."""
    filas = await _filas(
        con,
        "SELECT m.dispositivo_id, (m.metricas->>'retencion_real_dias')::float AS real,"
        " (m.metricas->>'retencion_proyectada_dias')::float AS proyectada"
        " FROM idcmd.metricas_actuales m WHERE m.sitio_id = %s"
        " AND m.metricas ? 'retencion_real_dias'",
        (sitio_id,),
    )
    nombres = {e["id"]: e for e in equipos}
    # La retención contratada se declara en el grabador; las cámaras la heredan.
    contratada_sitio = max(
        (e["retencion_contratada_dias"] for e in equipos if e["retencion_contratada_dias"]),
        default=None,
    )
    salida = []
    for f in filas:
        e = nombres.get(f["dispositivo_id"])
        if e is None:
            continue
        contratada = e["retencion_contratada_dias"] or contratada_sitio
        # Como en el agente: con disco lleno solo hay real; si no, manda la proyectada.
        juzgada = f["proyectada"] if f["proyectada"] is not None else f["real"]
        salida.append({
            "equipo": e["nombre"], "real": f["real"], "proyectada": f["proyectada"],
            "contratada": contratada,
            "cumple": None if contratada is None or juzgada is None else juzgada >= contratada,
        })  # fmt: skip
    salida.sort(key=lambda r: r["equipo"])
    return salida


async def _disponibilidad_guardada_o_calculada(
    con: AsyncConnection, sitio_id: uuid.UUID, periodo: date, ahora: datetime
) -> float | None:
    fila = await (
        await con.execute(
            "SELECT disponibilidad::float FROM idcmd.informes_mensuales"
            " WHERE sitio_id = %s AND periodo = %s",
            (sitio_id, periodo),
        )
    ).fetchone()
    if fila is not None:
        return fila[0]
    return await disponibilidad_sitio(con, sitio_id, periodo, ahora)
