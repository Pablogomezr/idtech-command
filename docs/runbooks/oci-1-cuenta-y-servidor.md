# Oracle Cloud, parte 1: cuenta, red, servidor y dominio

Esta parte se hace a mano en la consola de Oracle. Toma de 1 a 2 horas, más la espera de
verificación de la cuenta. La parte 2 (instalar IDtech Command en el servidor) usa los scripts de
`despliegue/oci/` y tiene su propia guía.

Al final vas a tener:

- una cuenta de Oracle Cloud con Bogotá como región principal;
- un servidor Ubuntu ARM con IP pública fija;
- el firewall abierto solo para la web (80/443), y SSH solo desde tu oficina;
- un dominio, por ejemplo `command.idtech.com.co`, apuntando al servidor;
- un bucket privado para los respaldos.

> **Nunca envíes por el chat claves, llaves privadas (`idcmd_oci` sin `.pub`) ni pantallazos donde
> se vean.** Para seguir solo hace falta que me digas "listo" y el nombre del dominio.

Los nombres de los botones pueden cambiar un poco porque Oracle actualiza la consola. Si alguno no
coincide, busca el más parecido.

---

## 1. Crear la cuenta (≈20 min + verificación)

1. Entra a <https://www.oracle.com/cloud/free/> → **Start for free**.
2. **País:** Colombia. Usa un correo que leas a diario (p. ej. el de IDtech), porque ahí llegan los
   avisos de cobro y de seguridad.
3. **Tipo de cuenta:** *Company*, con los datos de Ingeniería y Desarrollo IDtech SAS (NIT). Así las
   facturas salen a nombre de la empresa.
4. **Cloud Account Name:** algo corto, por ejemplo `idtech`. Es el nombre con que vas a ingresar.
5. **Home Region: `Colombia Central (Bogota)`**.
   ⚠️ **Esta es la decisión importante: no se puede cambiar después.** Los recursos gratuitos solo
   existen en la región principal, y el principio 4 del proyecto exige que los datos queden en
   Colombia.
6. **Tarjeta de crédito:** Oracle la pide para verificar la identidad. Hace un cobro temporal pequeño
   que luego reversa.
7. Espera el correo **"Your Oracle Cloud account is fully provisioned"**. Puede tardar desde minutos
   hasta unas horas.
8. Al primer ingreso, Oracle te obliga a activar el **segundo factor** de la consola. Usa la misma app
   de autenticación del portal.

## 2. Pasar a "Pay As You Go" y poner una alerta de gasto (≈5 min) — recomendado

Una cuenta solo gratuita tiene dos riesgos para un servicio 24/7:

- Oracle **recupera las máquinas gratuitas que ve "ociosas"**. Nuestro servidor usa poca CPU, así que
  podría parecer ocioso.
- Cuando hay mucha demanda, a las cuentas gratuitas se les niega capacidad ARM en Bogotá.

Con **Pay As You Go**, los recursos *Always Free* siguen sin costo, pero esos dos riesgos
desaparecen.

1. Menú ☰ → **Billing & Cost Management** → **Upgrade and Manage Payment** → **Upgrade to Pay As You
   Go**.
2. Menú ☰ → **Billing & Cost Management** → **Budgets** → **Create Budget**:
   - Nombre: `tope-idcmd`
   - Monto mensual: **USD 10**
   - Alerta al **50 %**, enviada a tu correo.

   Si algún día llega el correo, algo se creó fuera de lo gratuito y lo revisamos juntos.

## 3. Compartimento (≈2 min)

Menú ☰ → **Identity & Security** → **Compartments** → **Create Compartment**:

- Nombre: `idcmd`
- Descripción: `IDtech Command producción`

En todo lo que sigue, elige este compartimento en la lista de la izquierda antes de crear cada cosa.

## 4. Red (≈10 min)

1. Menú ☰ → **Networking** → **Virtual Cloud Networks** → **Start VCN Wizard** → **Create VCN with
   Internet Connectivity**.
   - Nombre: `idcmd-red`
   - Deja los rangos que trae por defecto → **Next** → **Create**.
2. Averigua la **IP pública de tu oficina**: desde un PC de la oficina abre <https://ifconfig.me> y
   anota el número.
3. En `idcmd-red` → **Subnets** → *Public Subnet-idcmd-red* → **Security Lists** → *Default Security
   List* → **Ingress Rules**:
   - **Edita** la regla del puerto **22**: cambia el origen `0.0.0.0/0` por
     `<IP-de-tu-oficina>/32`. Así solo tu oficina puede administrar el servidor.
   - **Add Ingress Rules**, una regla por puerto. Ambas con origen `0.0.0.0/0`, protocolo TCP:
     - destino **80** (lo usa Let's Encrypt para emitir el certificado; luego redirige a HTTPS);
     - destino **443** (el portal y la ingesta de los agentes).
   - No abras ningún otro puerto. Postgres nunca se publica.

> Si la IP de la oficina cambia (pasa con algunos proveedores de internet), el SSH dejará de
> conectar. Vuelve a este paso y actualiza la regla del 22. El portal sigue funcionando igual.

## 5. Llave SSH en tu PC (≈3 min)

En PowerShell, en tu PC:

```powershell
ssh-keygen -t ed25519 -f $HOME\.ssh\idcmd_oci -C "idcmd-oci"
```

- Te pide una **frase de paso**: pon una y guárdala en tu gestor de claves.
- Se crean dos archivos:
  - `idcmd_oci`: privado, **nunca se comparte**;
  - `idcmd_oci.pub`: público, este es el que se sube a Oracle.

Para copiar la parte pública:

```powershell
Get-Content $HOME\.ssh\idcmd_oci.pub | Set-Clipboard
```

## 6. El servidor (≈10 min)

Menú ☰ → **Compute** → **Instances** → **Create Instance**:

| Campo | Valor |
|---|---|
| Name | `idcmd-central` |
| Image | **Canonical Ubuntu 24.04** (Change image → Ubuntu) |
| Shape | Change shape → **Ampere** → `VM.Standard.A1.Flex` → **2 OCPU, 12 GB** de memoria |
| Networking | `idcmd-red`, subred **pública**, ✅ *Assign a public IPv4 address* |
| SSH keys | **Paste public keys** → pega lo que copiaste en el paso 5 |
| Boot volume | ✅ *Specify a custom boot volume size* → **100 GB** |

→ **Create**. En 1 o 2 minutos queda *Running*.

- **2 OCPU y 12 GB** alcanzan con holgura para varios sitios. Además caben dentro del límite gratuito
  vigente: en 2026 hay reportes de que Oracle bajó el límite de ARM gratuito de 4 OCPU/24 GB a
  2 OCPU/12 GB.
- Si aparece **"Out of capacity"**, cambia el *Availability/Fault domain* y vuelve a intentar, o
  inténtalo más tarde. Con Pay As You Go (paso 2) casi nunca pasa.

## 7. IP pública fija (≈5 min)

La IP que trae la máquina se pierde si algún día se recrea. Conviene reservarla, porque el dominio
apunta a ella. La primera IP reservada es gratuita.

1. Instancia → **Attached VNICs** → la VNIC → **IPv4 Addresses** → en la IP privada, menú ⋮ →
   **Edit**.
2. Marca **No public IP** → **Update**.
3. Vuelve a **Edit** → **Reserved public IP** → **Create a new reserved IP**, con nombre
   `idcmd-ip` → **Update**.
4. Anota esa IP. No hace falta que me la envíes.

## 8. Dominio (≈10 min + propagación)

En el proveedor donde tienes el dominio (el panel DNS de `idtech.com.co` o el que uses), crea este
registro:

| Tipo | Nombre | Valor | TTL |
|---|---|---|---|
| A | `command` | la IP reservada del paso 7 | 300 |

Si todavía no tienes dominio, cómpralo antes, por ejemplo en el registrador del `.com.co`. Sirve
cualquier dominio donde puedas crear registros A.

Para comprobar el registro, después de 5 a 30 minutos, en PowerShell:

```powershell
nslookup command.idtech.com.co
```

Debe responder con la IP reservada.

## 9. Probar la entrada al servidor (≈2 min)

Desde la oficina:

```powershell
ssh -i $HOME\.ssh\idcmd_oci ubuntu@command.idtech.com.co
```

- La primera vez pregunta si confías en el servidor: escribe `yes`.
- Luego pide la frase de paso de la llave.
- Si ves el indicador `ubuntu@idcmd-central:~$`, funcionó. Escribe `exit` para salir.

## 10. Bucket de respaldos (≈3 min)

Menú ☰ → **Storage** → **Buckets** → compartimento `idcmd` → **Create Bucket**:

- Nombre: `idcmd-respaldos`
- Default Storage Tier: **Standard**
- Encryption: *Encrypt using Oracle managed keys*
- Visibilidad: **privado** (es el valor por defecto; no lo cambies)

El enlace de solo escritura para que el servidor suba los respaldos lo creamos en la parte 2. Así,
aunque alguien tomara el servidor, no podría leer ni borrar las copias.

---

## Lista de chequeo

- [ ] Cuenta creada con región principal **Bogotá** y segundo factor activo
- [ ] Pay As You Go y alerta de USD 10 (recomendado)
- [ ] Compartimento `idcmd` y red `idcmd-red`
- [ ] Puerto 22 solo desde la IP de la oficina; 80 y 443 abiertos
- [ ] Instancia `idcmd-central` Ubuntu 24.04 ARM, 2 OCPU / 12 GB, 100 GB
- [ ] IP pública reservada
- [ ] Registro A del dominio apuntando a esa IP (`nslookup` lo confirma)
- [ ] `ssh` entra al servidor
- [ ] Bucket privado `idcmd-respaldos`

Cuando todo esté marcado, avísame con **"listo"** y el **nombre del dominio**. Seguimos con la
parte 2, que deja el portal publicado con HTTPS.
