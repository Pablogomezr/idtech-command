# ADR 0002 — Central alojada en Colombia: OCI Bogotá con Postgres y autenticación propios

- Estado: aceptada (2026-10-26). Reemplaza la propuesta inicial de Supabase São Paulo.

## Contexto
"Datos en Colombia" es un diferencial comercial frente a soluciones de fabricante (DoLynk
Care) y un posible requisito de entidades estatales: el inventario de seguridad de una entidad
(ubicaciones, IPs, modelos) podría considerarse información reservada (Ley 1712 de 2014).

## Decisión
- Oracle Cloud Infrastructure, región Bogotá (centro de datos de Claro), cuenta con Bogotá
  como *home region*.
- Una VM ARM con Docker compose: Caddy (TLS), app FastAPI, worker, Postgres 16.
- Autenticación propia en FastAPI: argon2id, TOTP, sesiones en servidor, sin auto-registro.
- Archivos (PDF, fotos, logos) en OCI Object Storage Bogotá, bucket privado, URLs firmadas.
- Respaldos: `pg_dump` cada 6 h + copia diaria cifrada en Object Storage Bogotá + segunda
  copia en NAS de IDtech (Barranquilla). Prueba de restauración mensual (runbook).

## Consecuencias
- Costo estimado USD 20–35/mes en piloto (confirmar con la calculadora de OCI).
- Operar la base queda a cargo de IDtech. Mitigante: los agentes conservan 7 días de lo ya
  enviado y pueden reenviarlo (`idcmd buzon reenviar --desde ...`).
- RLS con Postgres estándar: políticas sobre `current_setting('app.usuario_id')`; la app se
  conecta con un rol sin `BYPASSRLS`.
- Correo, Telegram y web push son servicios extranjeros: su contenido se minimiza (ADR 0003).
