# Supuestos aprobados (2026-10-26)

Pablo aprobó todos los supuestos ("ok"). Si alguno cambia, actualizar aquí y en el ADR afectado.

| # | Supuesto | Verificar en |
|---|---|---|
| P1/P6 | Central en OCI Bogotá con Postgres y autenticación propios; IDtech opera la base. | ADR 0002 |
| P2 | Milestone XProtect 2023 R1+ con API Gateway; BioStar 2.8+. Retención real por cámara se confirma con un spike (no hay endpoint REST documentado de grabación más antigua). | Etapa 0 |
| P3 | SLA 24×7; excluye mantenimiento y causas atribuibles al cliente; el tiempo sin datos se informa aparte y no cuenta como caída. Todo parametrizable por contrato. | ADR 0004, contrato real |
| P4 | La entidad final puede tener usuario de solo lectura limitado a sus sitios (RLS por alcance cliente/regional/sitio). | Entrega 3 |
| P5 | El agente soporta `HTTPS_PROXY` y CA corporativa desde el día 1. | Hecho (Entrega 2) |
| P7 | Informe al cliente por enlace al portal; adjunto PDF configurable por cliente. | ADR 0003 |
| P8 | PWA en línea con tolerancia (borradores, reintento de fotos); sin señal = Fase 2. | ADR 0003 |
| P9 | 30 min de verificación antes del cierre, configurable por contrato. | ADR 0004 |
| P10 | Dahua no entra al MVP salvo que el sitio piloto tenga muchos equipos Dahua. | Datos del sitio |

## Pendientes de Pablo (ruta crítica, no son código)
- Autorización escrita del cliente para instalar el agente.
- Datos del sitio piloto: versión/edición de Milestone y BioStar, cantidad de equipos por
  marca, salida a internet (directa o proxy), VLAN, ICMP permitido.
- Cuenta OCI con Bogotá como *home region* (no se puede cambiar después).
- Mini PC: Intel N100 o similar, 16 GB RAM, 512 GB SSD, TPM 2.0, idealmente 2 puertos de red.
- Reglas de SLA del contrato: tiempos de respuesta/solución por prioridad, horario.
- Contenido inicial de la base de conocimiento (`docs/plantillas/base_conocimiento.csv`).
