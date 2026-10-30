# Conectar un sitio a la central de producción

El agente de un sitio (PC con Docker Desktop o mini PC) envía el estado de los equipos a
`https://command.idtech.com.co`. Solo hace conexiones salientes por HTTPS; en el sitio no se abre
ningún puerto. Las claves de los equipos **no salen del sitio**: siguen en la bóveda local.

## Requisitos

- `inventario.yaml` del sitio, con el mismo formato que el laboratorio
  (`despliegue/laboratorio/inventario.ejemplo.yaml`).
- La bóveda del agente con las claves de los equipos (`idcmd credenciales guardar ...`).
- La llave SSH del servidor (`idcmd_oci`), estando en la oficina.

## Pasos (PowerShell, con Docker Desktop encendido)

```powershell
Expand-Archive $env:USERPROFILE\Downloads\idtech-command-vNN.zip -DestinationPath C:\ -Force
powershell -ExecutionPolicy Bypass -File C:\idtech-command-vNN\despliegue\sitio\conectar-sitio.ps1
```

Valores por defecto, para la oficina:

| Opción | Valor por defecto |
|---|---|
| `-Inventario` | `C:\idtech-lab\inventario.yaml` |
| `-Secretos` | `C:\idtech-lab\secretos` |
| `-Sitio` | `C:\idtech-sitio` |
| `-Nombre` | `oficina` |

Para otro sitio, pasa sus propias rutas y un `-Nombre` distinto.

Qué hace el script:

1. **Carga el inventario en la central** (`idcmd-central inventario cargar`). Crea el sitio y sus
   equipos, y crea el agente la primera vez; si se repite, reutiliza el mismo agente.
2. **Trae `agente.yaml` y `token_agente`** a `C:\idtech-sitio\agente`. No borra la copia del
   servidor, en `/srv/idcmd/sitios/<nombre>`, solo de root; sí borra la copia temporal en
   `/home/ubuntu`.
3. **Detiene el laboratorio local**, para no consultar dos veces los mismos equipos.
4. **Arranca el agente del sitio** (`despliegue/sitio/docker-compose.yml`).

La frase de la llave SSH se pide varias veces, una por cada conexión.

## Comprobar

En `https://command.idtech.com.co/portal` aparece el sitio. Los equipos toman su estado en unos
3 minutos: la detección confirma cada cambio con 3 chequeos.

Para ver el registro del agente:

```powershell
docker compose -f C:\idtech-command-vNN\despliegue\sitio\docker-compose.yml logs -f agente
```

En el registro deben verse líneas `POST https://command.idtech.com.co/api/v1/ingesta "HTTP/1.1 200 OK"`.

## Técnicos

Si el inventario nombra técnicos (`tecnicos:`) que no existen en la central, se crean primero:

```bash
sudo idcmd-central usuarios crear --correo ... --nombre "..." --rol tecnico
```

Luego se repite `conectar-sitio.ps1`. Mientras tanto, los tickets de ese sitio van a coordinación
(administradores).
