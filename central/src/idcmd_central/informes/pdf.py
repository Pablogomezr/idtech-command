"""HTML del informe -> PDF (WeasyPrint, ADR 0003). Se importa al usarlo: carga Pango."""

from __future__ import annotations


def a_pdf(html: str) -> bytes:
    from weasyprint import HTML

    # Sin base_url: el informe no carga nada externo (estilos y gráfico van dentro del HTML).
    return HTML(string=html).write_pdf()
