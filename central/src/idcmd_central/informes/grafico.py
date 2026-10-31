"""Gráfico de tendencia en SVG plano: se ve igual en el portal y en el PDF, sin JavaScript."""

from __future__ import annotations

from markupsafe import Markup, escape

from .informe import MESES, periodo_de

ANCHO, ALTO = 640, 200
MARGEN_IZQ, MARGEN_INF, MARGEN_SUP = 44, 28, 14


def _pct(v: float) -> str:
    return f"{v:.2f}".replace(".", ",")


def tendencia_svg(tendencia: list[dict], meta: float) -> Markup:
    """Barras de disponibilidad por mes con la línea de la meta. El eje empieza cerca del valor
    más bajo para que se noten diferencias de décimas (en SLA, 99,0 vs 99,9 importa)."""
    valores = [t["disponibilidad"] for t in tendencia if t["disponibilidad"] is not None]
    piso = min([meta, *valores]) if valores else meta
    piso = max(0.0, min(piso - 0.5, 99.0))
    piso = float(int(piso))  # número redondo
    alto_util = ALTO - MARGEN_INF - MARGEN_SUP
    ancho_util = ANCHO - MARGEN_IZQ - 10

    def y(v: float) -> float:
        return MARGEN_SUP + alto_util * (1 - (v - piso) / (100 - piso))

    paso = ancho_util / max(len(tendencia), 1)
    barra = paso * 0.56
    partes = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {ANCHO} {ALTO}" width="100%"'
        ' role="img" aria-label="Disponibilidad de los últimos meses" class="grafico">'
    ]
    for v in (piso, (piso + 100) / 2, 100.0):
        partes.append(
            f'<line x1="{MARGEN_IZQ}" x2="{ANCHO - 10}" y1="{y(v):.1f}" y2="{y(v):.1f}"'
            ' stroke="#e3e6eb" stroke-width="1"/>'
            f'<text x="{MARGEN_IZQ - 6}" y="{y(v) + 4:.1f}" text-anchor="end" font-size="10"'
            f' fill="#5d6677">{_pct(v).rstrip("0").rstrip(",")} %</text>'
        )
    for i, t in enumerate(tendencia):
        x = MARGEN_IZQ + paso * i + (paso - barra) / 2
        p = periodo_de(t["periodo"])
        etiqueta = f"{MESES[p.month - 1][:3]} {p.year % 100:02d}"
        partes.append(
            f'<text x="{x + barra / 2:.1f}" y="{ALTO - 8}" text-anchor="middle" font-size="11"'
            f' fill="#5d6677">{escape(etiqueta)}</text>'
        )
        v = t["disponibilidad"]
        if v is None:
            partes.append(
                f'<text x="{x + barra / 2:.1f}" y="{y(piso) - 6:.1f}" text-anchor="middle"'
                ' font-size="10" fill="#8a93a3">sin dato</text>'
            )
            continue
        color = "#1f7a3f" if v >= meta else "#b42318"
        ultimo = i == len(tendencia) - 1
        partes.append(
            f'<rect x="{x:.1f}" y="{y(max(v, piso)):.1f}" width="{barra:.1f}"'
            f' height="{y(piso) - y(max(v, piso)):.1f}" rx="3" fill="{color}"'
            f' opacity="{1 if ultimo else 0.55}"/>'
            f'<text x="{x + barra / 2:.1f}" y="{y(max(v, piso)) - 4:.1f}" text-anchor="middle"'
            f' font-size="11" font-weight="{700 if ultimo else 400}" fill="#1d2330">'
            f"{_pct(v)} %</text>"
        )
    partes.append(
        f'<line x1="{MARGEN_IZQ}" x2="{ANCHO - 10}" y1="{y(meta):.1f}" y2="{y(meta):.1f}"'
        ' stroke="#0b5cad" stroke-width="1.5" stroke-dasharray="5 4"/>'
        f'<text x="{MARGEN_IZQ + 4}" y="{y(meta) - 4:.1f}" font-size="10"'
        f' fill="#0b5cad">meta {_pct(meta)} %</text></svg>'
    )
    return Markup("".join(partes))
