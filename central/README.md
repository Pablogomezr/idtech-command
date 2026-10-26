# Central de IDtech Command

FastAPI + Postgres 16 con RLS. Hoy incluye la API de ingesta de los agentes
(`POST /api/v1/ingesta`, contrato en `../contrato/`); el portal y el worker vienen en las
siguientes entregas. Decisiones: ADR 0002 y 0003. Reglas de la base: `../CLAUDE.md`.

```bash
docker run -d --name idcmd-pg -e POSTGRES_PASSWORD=pruebas -p 55432:5432 postgres:16
uv sync && uv run pytest -q
```

Puesta en marcha de una base nueva (rol dueño):

```bash
export IDCMD_DSN_ADMIN=postgresql://idcmd_dueno:...@localhost/idcmd
uv run idcmd-central migrar
# Usuario de login de la aplicación (una vez, fuera de las migraciones):
#   CREATE ROLE idcmd_servicio LOGIN NOINHERIT PASSWORD '...';
#   GRANT idcmd_app, idcmd_ingesta TO idcmd_servicio;
uv run idcmd-central agentes crear --sitio <uuid> --nombre "Mini PC sede norte"
IDCMD_DSN=postgresql://idcmd_servicio:...@localhost/idcmd uv run uvicorn --factory idcmd_central.app:crear_app
```
