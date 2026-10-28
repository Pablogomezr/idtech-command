# Imagen de la central (API de ingesta + portal). Se construye desde la RAÍZ del repositorio:
#   docker build -f central/Dockerfile -t idcmd-central .
# (el lock de la central referencia ../agente solo para pruebas; no se instala en la imagen)
ARG IMAGEN_BASE=python:3.12-slim
FROM ${IMAGEN_BASE} AS construccion
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
WORKDIR /repo/central
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY agente/pyproject.toml /repo/agente/pyproject.toml
COPY central/pyproject.toml central/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY central/src ./src
RUN uv sync --locked --no-dev --no-editable

FROM ${IMAGEN_BASE}
RUN groupadd --system --gid 10001 idcmd \
    && useradd --system --uid 10001 --gid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin idcmd
COPY --from=construccion /repo/central/.venv /repo/central/.venv
COPY central/migraciones /app/migraciones
ENV PATH=/repo/central/.venv/bin:$PATH PYTHONUNBUFFERED=1 IDCMD_MIGRACIONES=/app/migraciones
USER 10001
EXPOSE 8000
CMD ["uvicorn", "idcmd_central.app:crear_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
