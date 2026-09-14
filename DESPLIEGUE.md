# Guía de despliegue

Bitácora operativa del despliegue en la nube. Está escrita a partir de los
problemas **reales** encontrados, porque ninguno de ellos es obvio a primera
vista y todos responden de forma confusa.

Complementa a `backend-nube/README.md` (referencia del servicio) y a
`CONTRATO_NUBE.md` (diseño de la arquitectura).

---

## 1. Qué se despliega y dónde

| Componente | Dónde corre | Cómo se despliega |
|---|---|---|
| **Worker** | PC junto a la cámara | `cd worker && python main.py` |
| **Concentrador** | PC de la planta (uno solo) | `cd concentrador && python main.py` |
| **Backend nube** | Vercel (serverless) | `node deploy.mjs deploy` |
| **Base de datos** | Supabase (PostgreSQL) | SQL Editor (una sola vez) |

Solo el **backend nube** es un despliegue propiamente tal: es lo único que sale
de tu máquina. El worker y el concentrador se ejecutan localmente.

---

## 2. Puesta en marcha en orden

El orden importa: cada paso depende del anterior.

### Paso 1 — Base de datos (Supabase)

1. Crear el proyecto en <https://supabase.com>.
2. **SQL Editor** → ejecutar el contenido completo de `backend-nube/schema.sql`.

   ⚠️ `schema.sql` crea las tablas, pero **no otorga permisos**. Sin el paso 3
   la API responde `permission denied` aunque las credenciales sean correctas.

3. **SQL Editor** → otorgar permisos al rol del backend:

```sql
GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO service_role;
GRANT ALL ON SCHEMA public TO service_role;
GRANT USAGE ON SCHEMA public TO anon, authenticated;
```

### Paso 2 — Obtener credenciales

En **Supabase → Settings → API Keys**:

| Credencial | Nombre actual en el panel | Para qué |
|---|---|---|
| **Project URL** | API URL (`https://<ref>.supabase.co`) | `SUPABASE_URL` |
| **Secret key** | empieza con `sb_secret_...` | `SUPABASE_SERVICE_KEY` |

> **Ojo con los nombres.** Supabase renombró estos conceptos y la documentación
> vieja ya no coincide. Lo que antes se llamaba `service_role` hoy aparece como
> **Secret key** (`sb_secret_...`), y la `anon key` es la **Publishable key**
> (`sb_publishable_...`). El backend necesita la **Secret key**: salta RLS y va
> solo en el servidor.

### Paso 3 — Variables de entorno en Vercel

Crear `backend-nube/.env` (está gitignored):

```env
SUPABASE_URL=https://<ref>.supabase.co
SUPABASE_SERVICE_KEY=sb_secret_...
CONCENTRADOR_TOKEN=<token largo y aleatorio>
```

Subirlas a Vercel. La CLI no acepta tuberías de forma cómoda en Windows, así que
se pasan leyendo del `.env`:

```bash
cd backend-nube

npx vercel env add SUPABASE_URL production
npx vercel env add SUPABASE_SERVICE_KEY production
npx vercel env add CONCENTRADOR_TOKEN production
```

Cada comando **pide el valor por entrada estándar** y queda esperando. Verificar
que quedaron cargadas:

```bash
npx vercel env ls production
```

### Paso 4 — Autenticarse contra Vercel

**Usar token, no el flujo interactivo.** El device flow (`vercel login`) falla
con `Error: Verification token was not provided` porque el código que muestra en
pantalla hay que tipearlo en una URL específica, y en Windows terminal no se
copian bien los caracteres.

```bash
npx vercel login                    # ❌ falla en Windows
npx vercel --token <TOKEN> ...      # ✅ usar token
```

El token se crea en <https://vercel.com/account/tokens>. En este proyecto se
guarda en `backend-nube/.vercel-token` (gitignored) y lo consume `deploy.mjs`.

```bash
node deploy.mjs whoami    # verifica la sesión
node deploy.mjs deploy    # despliega a producción
```

### Paso 5 — Conectar el concentrador

El token **no va en `config.yaml`** (está versionado). Va en
`concentrador/nube.key` (gitignored):

```bash
echo "<CONCENTRADOR_TOKEN>" > concentrador/nube.key
```

Y en `concentrador/config.yaml`:

```yaml
nube:
  enabled: true
  base_url: "https://backend-nube.vercel.app"
  token: ""                  # se resuelve de nube.key o del entorno
  concentrador_id: "concentrador-1"
```

El orden de resolución del token es:
**`CONCENTRADOR_TOKEN` (entorno) → `nube.key` → `config.yaml`**.

---

## 3. Los tres problemas que cuestan tiempo

Ninguno da un mensaje de error que apunte a la causa real.

### 3.1 · La API responde `401 token inválido`

**Síntoma:** el token es correcto, está cargado en Vercel, y aun así toda
llamada autenticada falla.

**Causa:** el handler estaba declarado con `runtime: 'edge'`. En Edge las
variables de entorno **no llegaban al código de forma consistente**: el valor se
leía como vacío en unos puntos y correcto en otros.

**Diagnóstico:** agregar un endpoint temporal que devuelva las longitudes de las
variables de entorno y las cabeceras recibidas. Si la longitud del token
configurado es `0`, el problema es la inyección de variables, no el valor.

**Solución:** usar el runtime **Node.js** (`runtime: 'nodejs'`). El handler
recibe `(req, res)` de `@vercel/node` y se adapta a la interfaz tipo Fetch que
usa el resto del código.

> Regla práctica: en Vercel, si el comportamiento de las variables de entorno
> resulta errático, sospechar del runtime Edge antes que del valor.

### 3.2 · La API responde `permission denied for table <tabla>`

**Síntoma:**

```json
{"code":"42501","message":"permission denied for table config_workers"}
```

**Causa:** en Supabase, `service_role` **no recibe permisos automáticamente**
sobre las tablas creadas por SQL Editor. Tener la Secret key correcta no alcanza:
el rol necesita `GRANT` explícito.

**Solución:** el bloque `GRANT` del Paso 1.3.

**Diagnóstico sin pasar por la API:** consultar Supabase directamente con la key
para aislar si el problema es de credenciales o de permisos:

```bash
curl "https://<ref>.supabase.co/rest/v1/config_workers?select=*" \
  -H "apikey: sb_secret_..." \
  -H "Authorization: Bearer sb_secret_..."
```

Si esto devuelve `permission denied`, el problema está en Supabase y no en
Vercel.

### 3.3 · La API responde `404 NOT_FOUND` (con `gru1::...`)

**Síntoma:** el endpoint responde `404` con un cuerpo que parece de Vercel:

```
The page could not be found

NOT_FOUND

gru1::hs5m6-1789404672063-32e5be665ccd
```

**Causa más probable (A): el deploy se hizo desde la carpeta equivocada.**
Este es un monorepo: la API vive en `backend-nube/`, no en la raíz. Si se corre
`vercel --prod` desde la raíz del repo, Vercel publica una carpeta sin la función
`api/` y **pisa el deploy bueno**. El servicio queda caído.

**Cómo reconocerlo:** el deploy tarda **2-3 s** en vez de los ~13 s de un build
real. Mirar con:

```bash
cd backend-nube
npx vercel ls --prod
```

**Solución:** desplegar siempre desde `backend-nube/`:

```bash
cd backend-nube && node deploy.mjs deploy
```

**Causa más probable (B): Deployment Protection activado.** Si en vez de `404`
llega un `302` que redirige a `vercel.com/sso-api`, el proyecto exige
autenticación de Vercel y el concentrador no puede entrar. El síntoma que ve el
concentrador es confuso: reporta `404` porque sigue el redirect y recibe la
página HTML de error.

Comprobar el redirect:

```bash
curl -sI https://<deploy>.vercel.app/api/salud
# Location: https://vercel.com/sso-api?url=...  → protección activa
```

**Solución:** <https://vercel.com/<equipo>/<proyecto>/settings/deployment-protection>
→ **Vercel Authentication: Disabled**.

> ⚠️ Esta protección **rompe la integración con el concentrador** sin dar un
error claro. Si en el futuro el servicio deja de responder de un momento a otro,
revisar esto antes de sospechar del código.

### 3.4 · La CLI de Vercel no acepta el token

**Síntoma:**

```
Error: You defined "--token", but its contents are invalid.
Must not contain: "-"
```

**Causa:** la CLI **sí** acepta guiones; lo que ocurre es que `vercel env ls`
y otros subcomandos interpretan el argumento de forma distinta según la posición.
El error aparece cuando el token se pasa a un subcomando que no lo espera ahí.

**Solución:** pasar el token con `--token` en el subcomando correcto, o usar el
script `deploy.mjs` que lo lee del archivo y lo coloca en la posición adecuada.

---

## 4. Verificación del despliegue

Ejecutar en orden. Si un paso falla, el siguiente tampoco va a funcionar.

```bash
# 1. ¿El servicio está vivo?
curl https://backend-nube.vercel.app/api/salud
# → {"ok":true,"servicio":"poccam-backend-nube"}

# 2. ¿Autentica y llega a la base?
curl -H "Authorization: Bearer <CONCENTRADOR_TOKEN>" \
  "https://backend-nube.vercel.app/api/concentrador/config?version=-1"
# → {"version":0,"workers":{}}

# 3. ¿Rechaza sin token?
curl "https://backend-nube.vercel.app/api/concentrador/config?version=-1"
# → 401 {"error":"token inválido"}
```

**Prueba de escritura** (crea un análisis ficticio):

```bash
curl -X POST https://backend-nube.vercel.app/api/concentrador/analisis \
  -H "Authorization: Bearer <CONCENTRADOR_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"worker_id":"panel-1","evento_id":"test_1","datos":{"n":1}}'
# → {"ok":true,"recibido":"test_1"}
```

Comprobar que quedó guardado:

```bash
curl "https://backend-nube.vercel.app/api/analisis?limit=5"
```

**Idempotencia**: reenviar el mismo `evento_id` dos veces debe dejar **un solo**
registro. Es lo que hace segura la cola offline del concentrador.

---

## 5. Despliegue continuo

`vercel --prod` despliega **lo que hay en la carpeta local**, independiente de
git. Si se conecta el repositorio en el dashboard de Vercel, cada push a
`master` redespliega automáticamente.

⚠️ En ese caso hay que configurar **Root Directory = `backend-nube`**, porque es
un monorepo y la API no está en la raíz.

> ⚠️ **Nunca correr `vercel --prod` desde la raíz del monorepo.** Publica una
> carpeta sin la función `api/` y **pisa la producción**. Ver §3.3-A.

**Las variables de entorno no se versionan**: al recrear el proyecto en Vercel
hay que volver a cargarlas (Paso 3).

---

## 6. Problemas conocidos y sus límites

| Síntoma | Causa | Qué hacer |
|---|---|---|
| `Faltan SUPABASE_URL o ...` | Variables no cargadas en el proyecto de Vercel | Paso 3 |
| `token inválido` con token correcto | Runtime Edge | §3.1 |
| `permission denied` | Faltan `GRANT` | §3.2 |
| `404 NOT_FOUND` (deploy de 2-3 s) | Deploy desde la carpeta equivocada | §3.3-A |
| `302` hacia `vercel.com/sso-api` | Deployment Protection activo | §3.3-B |
| `deploy.mjs` no encuentra el token | Falta `.vercel-token` | Paso 4 |
| El concentrador no baja config | `enabled: false` o token no resuelto | Paso 5 |
| Config llega pero no se aplica | Worker desconocido (revisar `worker_id`) | `config.yaml` del concentrador |

---

## 7. Credenciales: qué rotar y cuándo

Estas credenciales circulan en texto plano y conviene rotar de forma periódica,
sobre todo si pasaron por un chat, un log o una captura de pantalla:

| Credencial | Dónde se rota | Quién la usa |
|---|---|---|
| `SUPABASE_SERVICE_KEY` | Supabase → Settings → API Keys | Solo el backend nube |
| `CONCENTRADOR_TOKEN` | Se genera de nuevo | Concentrador ↔ nube |
| Token de Vercel | <https://vercel.com/account/tokens> | Solo despliegue |
| API key de DeepSeek | Panel de DeepSeek | Solo el worker (`worker/ia.key`) |

**Al rotar el token del concentrador hay que actualizar los dos extremos**: la
variable en Vercel y el archivo `nube.key`. Si solo se cambia uno, la API
responde `401`.

Los archivos que nunca deben llegar a git: `.env`, `*.key`, `.vercel-token`.
Verificar antes de cada push con `git --no-optional-locks status --short`.
