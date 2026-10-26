# IDtech Command

Monitoreo de salud multimarca de CCTV y control de acceso, gestión de correctivos e informe
mensual de SLA. Producto de Ingeniería y Desarrollo IDtech SAS.

- `agente/` — agente en sitio (Python 3.12, Docker): solo lectura, conexión solo saliente,
  credenciales cifradas que nunca salen del sitio.
- `central/` — portal, ingesta y worker (en construcción).
- `contrato/` — esquema JSON del contrato agente → central.
- `docs/` — decisiones (ADR), supuestos, runbooks y plantillas.

Empiece por [`CLAUDE.md`](CLAUDE.md).

```bash
cd agente && uv sync && uv run pytest -q
```
