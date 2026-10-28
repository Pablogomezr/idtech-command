# Imagen única del agente IDtech Command. Se configura solo con archivos montados.
# En cada versión de producción, fijar las imágenes base por digest (@sha256:...).
ARG IMAGEN_BASE=python:3.12-slim
FROM ${IMAGEN_BASE} AS construccion
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM ${IMAGEN_BASE}
RUN groupadd --system --gid 10001 idcmd \
    && useradd --system --uid 10001 --gid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin idcmd
# /datos (buzón) debe existir con dueño 10001: un volumen nuevo copia la propiedad del
# directorio de la imagen; sin esto Docker lo crea de root y el agente no puede escribir.
RUN mkdir -p /datos && chown 10001:10001 /datos
COPY --from=construccion /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
USER 10001
ENTRYPOINT ["idcmd", "--config", "/config/agente.yaml"]
CMD ["ejecutar"]
