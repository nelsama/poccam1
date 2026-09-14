# Backend nube — API del sistema externo

API serverless (Vercel · Node/TypeScript) que implementa el lado servidor del
contrato con el concentrador (`CONTRATO_NUBE.md`) y persiste en **Supabase**.

## Responsabilidad

| Función | Endpoint |
|---|---|
| Entregar configuración al concentrador | `GET /api/concentrador/config?version=N` |
| Recibir análisis de la IA | `POST /api/concentrador/analisis` |
| Recibir heartbeat (estado de workers) | `POST /api/concentrador/estado` |
| Recibir config publicada localmente | `POST /api/concentrador/config` |
| Listar workers conocidos | `GET /api/workers` |
| Consultar análisis | `GET /api/analisis?worker_id=...&limit=N` |
| Consultar eventos | `GET /api/eventos?worker_id=...&limit=N` |
| Salud del servicio | `GET /api/salud` |

> Esta API **no expone imagen de cámara**. Solo JSON de configuración,
> eventos y análisis.

## Puesta en marcha

### 1. Crear las tablas en Supabase

En **Supabase → SQL Editor**, ejecutar el contenido de `schema.sql`.

### 2. Variables de entorno

Copiar `.env.example` a `.env` y completar:

| Variable | De dónde sale |
|---|---|
| `SUPABASE_URL` | Supabase → Settings → API → Project URL |
| `SUPABASE_SERVICE_KEY` | Supabase → Settings → API → service_role key |
| `CONCENTRADOR_TOKEN` | Lo generás vos (largo y aleatorio) |

### 3. Desarrollo local

```bash
npm install
npx vercel dev          # levanta en http://localhost:3000
```

### 4. Despliegue en Vercel

```bash
npx vercel --prod
```

O desde el dashboard de Vercel: importar el repo y **configurar el
"Root Directory" como `backend-nube`**.

Las variables de entorno se configuran en
**Vercel → Project → Settings → Environment Variables** (los mismos 3 valores).

### 5. Conectar el concentrador

En `concentrador/config.yaml`:

```yaml
nube:
  enabled: true
  base_url: "https://tu-proyecto.vercel.app"
  token: "el-mismo-CONCENTRADOR_TOKEN"
  concentrador_id: "planta-1"
```

## Probar rápido

```bash
# Salud
curl https://tu-proyecto.vercel.app/api/salud

# Config (con token)
curl -H "Authorization: Bearer TU_TOKEN" \
  "https://tu-proyecto.vercel.app/api/concentrador/config?version=-1"

# Análisis recibidos
curl "https://tu-proyecto.vercel.app/api/analisis?limit=10"
```

## Notas de arquitectura

- **Serverless**: no hay proceso permanente ni filesystem. Toda la persistencia
  es en Supabase.
- **`datos` es JSONB**: el JSON libre que devuelve la IA se guarda tal cual, así
  el esquema puede cambiar sin migrar la base.
- **Auth por token**: el concentrador se autentica con `Bearer`. La
  `service_role` key nunca sale del servidor.
- **Idempotencia**: `analisis.evento_id` es `UNIQUE` — si el concentrador
  reintenta un envío (cola offline), no se duplica.
