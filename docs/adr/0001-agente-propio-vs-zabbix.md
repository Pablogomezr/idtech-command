# ADR 0001 — Agente propio en Python en lugar de Zabbix

- Estado: aceptada (2026-10-26)

## Contexto
Se evaluó usar Zabbix (servidor + Zabbix proxy en sitio) para la recolección y construir
encima solo el portal, los tickets y el informe de SLA.

## Decisión
Construir un agente propio en Python 3.12 (Docker), con adaptadores extensibles.

## Razones
1. **Credenciales**: en Zabbix las macros secretas viven en la base del servidor; incluso con
   un vault, el servidor obtiene los valores y los envía al proxy en cada sincronización.
   Incumple el criterio "ninguna credencial de dispositivo existe en la central".
2. **Red**: el protocolo Zabbix usa TCP 10051, que no atraviesa proxies HTTP corporativos.
   El agente usa HTTPS 443 estándar con `HTTPS_PROXY` y CA corporativa.
3. **Lo que Zabbix ahorra es lo barato** (ping, SNMP). Milestone y BioStar 2 habría que
   escribirlos igual, y los tickets, MTTR, atribución y PDF también.
4. **Operación**: dos sistemas, dos inventarios por sincronizar y ≈ USD 20–25/mes extra.

## Consecuencias
- Hay que escribir ICMP/ONVIF/SNMP (≈ 1 semana).
- Zabbix puede entrar después como *adaptador de fuente* para clientes que ya lo tengan.
