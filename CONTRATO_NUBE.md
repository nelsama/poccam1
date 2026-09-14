# Contrato: Concentrador ↔ Sistema Externo (nube)

Documento de diseño previo a la implementación. Define cómo se comunican el
**concentrador local** (que agrega los workers de cada cámara) con el
**sistema externo** (backend + frontend en la nube).

Complementa a `API.md` (contrato del worker) y `ARQUITECTURA.md` (diseño actual).

---

## 1. Arquitectura objetivo

```
┌──────────────── SISTEMA EXTERNO (nube: back + front) ────────────────┐
│  • Frontend / dashboards (por cámara, por esquema, globales)          │
│  • Backend/API: fuente de verdad de configuración                     │
│  • Base de datos: config, eventos, análisis, estadísticas             │
└───────────────────────────▲──────────────────────────────────────────┘
                            │  UNA sola conexión (saliente del concentrador)
                            │  ▲ eventos + análisis
                            │  ▼ configuración
┌───────────────────────────┴──────────────────────────────────────────┐
│                        CONCENTRADOR (local)                           │
│  • Conoce el mapa `worker_id → URL local`                             │
│  • Recolecta eventos/análisis de los workers (SSE)                    │
│  • Reparte la configuración a cada worker                             │
│  • Estadísticas locales + cola offline                                │
│  • (opcional) Front local unificado                                   │
└───────────▲──────────────────▲──────────────────▲────────────────────┘
            │                  │                  │  red local / loopback
      ┌─────┴────┐       ┌─────┴────┐       ┌─────┴────┐
      │ Worker   │       │ Worker   │       │ Worker   │  ← sin cambios
      │ cam 1    │       │ cam 2    │       │ cam 3    │
      └──────────┘       └──────────┘       └──────────┘
```

**Principios:**

1. El concentrador **solo inicia conexiones salientes** → funciona detrás de
   NAT, sin IP pública, sin túneles.
2. El sistema externo **nunca accede a las cámaras**.
3. Los **workers no cambian**: siguen exponiendo la API de `API.md`.
4. La **identidad se define localmente** y no se modifica remotamente.

---

## 2. Identidad: `worker_id`

Cada worker tiene un identificador **único, estable y legible**:

```yaml
# config.yaml del worker
captura:
  worker_id: "panel-lavado-1"        # IDENTIDAD (fija, no se cambia desde afuera)
  nombre_camara: "Panel Lavado 1"    # etiqueta de presentación (cosmética)
```

| Concepto | Rol | ¿Configurable desde la nube? |
|---|---|---|
| `worker_id` | Identidad del worker | ❌ No (es local) |
| `nombre_camara` | Cómo se muestra | ✅ Sí |

**Reglas del `worker_id`:**

- Único en la instalación (no puede repetirse).
- Estable entre reinicios (vive en `config.yaml`).
- No depende de IP ni puerto (esos cambian; el ID no).
- Legible para operación: `panel-secado-2` mejor que un UUID.
- Multi-planta: prefijo de sitio, p. ej. `planta-norte/panel-secado-2`.

**Mapa del concentrador** (es quien conoce las direcciones locales):

```yaml
# config.yaml del concentrador
workers:
  - id: "panel-lavado-1"
    url: "http://127.0.0.1:5001"
  - id: "panel-secado-1"
    url: "http://127.0.0.1:5002"
```

---

## 3. Sobre genérico de los datos de salida (IA)

El contrato de transporte es **fijo**; el **contenido es libre** (definido por el
prompt de cada worker). Así el sistema externo puede albergar dashboards
distintos sin cambiar el contrato.

### Estructura del sobre

```json
{
  "tipo": "analisis_ia",
  "worker_id": "panel-lavado-1",
  "evento_id": "20260910_143022_123",
  "timestamp": "2026-09-10T14:30:22.123-04:00",
  "esquema": "personas_v1",
  "datos": { },
  "meta": {
    "modelo": "deepseek-v4-flash-vision-exp",
    "detail": "low",
    "area_px": 547,
    "score": 0.0193,
    "area_borde": 0,
    "latencia_ms": 1240
  }
}
```

| Campo | Tipo | Descripción |
|---|---|---|
| `tipo` | string | Tipo de mensaje: `analisis_ia` \| `evento` \| `estado` |
| `worker_id` | string | Origen (identidad del worker) |
| `evento_id` | string | ID del evento que disparó el análisis |
| `timestamp` | string | ISO-8601 con zona horaria |
| `esquema` | string | Identificador del formato de `datos` (definido por el worker) |
| `datos` | object | **JSON libre**: exactamente lo que devolvió la IA |
| `meta` | object | Metadatos del sistema (modelo, área, score, latencia…) |

**Clave del diseño:** `datos` es **opaco** para el transporte. El sistema
externo lo guarda y lo muestra; no necesita conocer sus campos de antemano.

### El campo `esquema`

Es un **nombre libre** que declara "qué estructura tiene `datos`" para ese
worker. Se define en la config del worker:

```yaml
ia:
  esquema: "personas_v1"
```

Sirve para que el sistema externo pueda:
- Filtrar/vistas por tipo de análisis
- Validar (si quiere) contra un JSON Schema
- Renderizar la UI de forma específica cuando lo conozca

---

## 4. Ejemplos por worker

### Worker A — `panel-lavado-1` (detección de personas)

**Prompt del worker:**
```
Analiza la imagen. Responde SOLO con un JSON con esta estructura exacta:
{
  "hay_persona": false,
  "descripcion": "",
  "confianza": 0.0
}
```

**Mensaje enviado al sistema externo:**
```json
{
  "tipo": "analisis_ia",
  "worker_id": "panel-lavado-1",
  "evento_id": "20260910_143022_123",
  "timestamp": "2026-09-10T14:30:22.123-04:00",
  "esquema": "personas_v1",
  "datos": {
    "hay_persona": true,
    "descripcion": "una persona con uniforme azul frente al panel",
    "confianza": 0.92
  },
  "meta": {
    "modelo": "deepseek-v4-flash-vision-exp",
    "detail": "low",
    "area_px": 547,
    "score": 0.0193,
    "area_borde": 0,
    "latencia_ms": 1240
  }
}
```

### Worker B — `panel-secado-1` (lectura de display)

**Prompt del worker:**
```
Analiza la imagen. Responde SOLO con un JSON con esta estructura exacta:
{
  "valor_display": "",
  "unidad": "",
  "estado": ""
}
```

**Mensaje enviado al sistema externo:**
```json
{
  "tipo": "analisis_ia",
  "worker_id": "panel-secado-1",
  "evento_id": "20260910_143101_456",
  "timestamp": "2026-09-10T14:31:01.456-04:00",
  "esquema": "display_v1",
  "datos": {
    "valor_display": "083",
    "unidad": "°C",
    "estado": "calentando"
  },
  "meta": {
    "modelo": "deepseek-v4-flash-vision-exp",
    "detail": "low",
    "area_px": 310,
    "score": 0.0221,
    "area_borde": 0,
    "latencia_ms": 980
  }
}
```

> El **sobre es idéntico** en ambos casos. Solo cambian `worker_id`, `esquema` y
> `datos`. Eso es lo que permite un sistema externo genérico.

---

## 5. Endpoints (concentrador → sistema externo)

Todas las peticiones **las inicia el concentrador**. Autenticación por token en
cabecera: `Authorization: Bearer <token>` (el token vive en un archivo local, no
en git).

### 5.1 Bajar configuración

```http
GET /api/concentrador/config?version=<n>
Authorization: Bearer <token>
```

| Respuesta | Significado |
|---|---|
| `200` con `{"version": N, "workers": {...}}` | Hay config nueva (o la vigente) |
| `304 Not Modified` | Sin cambios desde `<n>` (ahorro de ancho de banda) |

```json
{
  "version": 42,
  "workers": {
    "panel-lavado-1": {
      "captura":   { "intervalo_segundos": 0.5, "rotacion": 90 },
      "deteccion": { "min_area_px": 350, "blur_ksize": 9 },
      "ia":        { "esquema": "personas_v1", "prompt": "..." }
    },
    "panel-secado-1": {
      "captura":   { "intervalo_segundos": 1.0, "rotacion": 0 },
      "deteccion": { "min_area_px": 700, "blur_ksize": 11 }
    }
  }
}
```

> El concentrador aplica cada bloque al worker correspondiente mediante su
> `POST /api/externo/config` (ver `API.md`). Los parámetros **no incluidos** se
> dejan como están (parcial).
>
> ⚠️ **DECIDIDO: la cámara SOLO se configura localmente.**
> `camara_fuente` y `fuente` **no** se gestionan desde la nube ni desde el
> concentrador: son hardware de la máquina y solo se ajustan desde el front
> local del worker. El endpoint externo (`/api/externo/config`) **ignora**
> esos campos aunque vengan en el payload.

### 5.2 Subir análisis / eventos

```http
POST /api/concentrador/analisis
Authorization: Bearer <token>
Content-Type: application/json

{ ...sobre genérico de la sección 3... }
```

Respuesta: `{"ok": true, "recibido": "<evento_id>"}`

El concentrador **encola** si falla (ver §7) y reintenta.

### 5.3 Reportar estado (heartbeat)

```http
POST /api/concentrador/estado
Authorization: Bearer <token>
```

```json
{
  "concentrador_id": "planta-norte",
  "timestamp": "2026-09-10T14:31:00-04:00",
  "workers": {
    "panel-lavado-1": { "camara_viva": true, "resolucion": [640, 480],
                        "ia_activa": false, "capturas": 1234, "eventos": 12 },
    "panel-secado-1": { "camara_viva": true, "resolucion": [1920, 1080],
                        "ia_activa": true,  "capturas": 980,  "eventos": 7 }
  }
}
```

Permite que el dashboard remoto muestre el estado **aunque el concentrador no
sea alcanzable** (nunca lo es).

### 5.4 (Opcional) Publicar cambios hechos en local

```http
POST /api/concentrador/config
Authorization: Bearer <token>
```

Con el mismo formato de §5.1. Sirve para que los cambios hechos desde el
**front local** queden registrados en la nube (fuente de verdad).

---

## 6. Modelo de datos sugerido (sistema externo)

El campo `datos` se guarda como **JSONB** para no atar la base al esquema.

```sql
CREATE TABLE eventos (
  id           BIGSERIAL PRIMARY KEY,
  worker_id    TEXT        NOT NULL,
  evento_id    TEXT        UNIQUE,
  timestamp    TIMESTAMPTZ NOT NULL,
  score        REAL,
  area_px      INTEGER,
  area_borde   INTEGER,
  metodo       TEXT
);

CREATE TABLE analisis (
  id           BIGSERIAL PRIMARY KEY,
  worker_id    TEXT        NOT NULL,
  evento_id    TEXT        REFERENCES eventos(evento_id),
  timestamp    TIMESTAMPTZ NOT NULL,
  esquema      TEXT,              -- "personas_v1", "display_v1", ...
  datos        JSONB,             -- JSON libre que devolvió la IA
  modelo       TEXT,
  detail       TEXT,
  latencia_ms  INTEGER,
  error        TEXT               -- si el análisis falló
);

CREATE INDEX idx_analisis_worker  ON analisis (worker_id, timestamp DESC);
CREATE INDEX idx_analisis_esquema ON analisis (esquema);
CREATE INDEX idx_analisis_datos   ON analisis USING GIN (datos);
```

Consultas habilitadas por el diseño genérico:

```sql
-- Todos los análisis del panel de secado
SELECT * FROM analisis WHERE worker_id = 'panel-secado-1'
ORDER BY timestamp DESC LIMIT 50;

-- Filtrar dentro del JSON libre
SELECT timestamp, datos->>'valor_display'
FROM analisis
WHERE esquema = 'display_v1'
  AND datos->>'estado' = 'calentando';
```

---

## 7. Estrategia de persistencia por nivel

| Nivel | Qué guarda | Dónde | ¿Base de datos? |
|---|---|---|---|
| **Worker** | Config, ROI, prompt, historial de prompts | Archivos (`config.yaml`, `roi.json`, `ia_prompt.txt`) | ❌ No (volumen mínimo, portabilidad) |
| **Worker** | Eventos y análisis locales | `eventos.jsonl`, `analisis_ia/*.json` | ❌ No (se mantienen; el concentrador los recolecta) |
| **Concentrador** | Estadísticas, cola offline, histórico local | **SQLite** | ✅ Sí (un archivo, sin infraestructura) |
| **Concentrador** | Cola de pendientes si no hay internet | `pendientes_nube.jsonl` | ❌ Archivo (simple y robusto) |
| **Nube** | Config, eventos, análisis, usuarios | **PostgreSQL** (o el que se elija) | ✅ Sí |
| **Nube** | Imágenes de eventos (a futuro) | Object storage (S3/MinIO) | ✅ Cuando crezca el volumen |

**Criterio:** archivos donde el volumen es mínimo y la simplicidad manda; base
de datos donde los datos crecen, se consultan o alimentan dashboards.

> El worker **puede seguir escribiendo archivos** aunque exista la base de datos
> en otro nivel (doble escritura): así nada se rompe y la migración es gradual.

---

## 8. Flujo completo (resumen)

```
CONFIGURACIÓN (de la nube hacia la cámara)
  Front nube → Backend nube (guarda, version++) 
             ← Concentrador pregunta (GET config?version=N)
             ← Concentrador reparte: POST /api/config a cada worker
             ← Worker aplica en vivo (ya implementado)

RESULTADOS (de la cámara hacia la nube)
  Worker detecta cambio → análisis IA → /api/analisis + SSE
             → Concentrador recolecta (SSE)
             → POST /api/concentrador/analisis (con worker_id + esquema + datos)
             → Backend nube guarda en DB (datos como JSONB)
             → Front nube muestra según dashboard
```

---

## 9. Decisiones pendientes / a confirmar

| Tema | Opciones | Recomendación |
|---|---|---|
| Fuente de verdad de la config | (A) nube manda · (B) local manda | **(A)** nube manda; el front local publica sus cambios |
| Mecanismo de bajada de config | Polling con `version` · SSE saliente | **Polling + etag** (robusto) y SSE como mejora |
| Parámetros de máquina por API | Incluir o excluir `camara_fuente`/`web_port` | **Excluir (DECIDIDO)**: la cámara solo se configura desde el front local |
| Enviar imágenes a la nube | JSON solo · JSON + imagen | JSON primero; imágenes después si hace falta |
| Multi-esquema por worker | Un esquema fijo · varios | Uno por worker (simple); varios a futuro |
| Auth | Sin auth · token · token + HTTPS | **Token** (el endpoint está expuesto a internet) |

---

## 10. Fases de implementación

| Fase | Alcance | Toca al worker? |
|---|---|---|
| **1** | `CONTRATO` + campo `esquema` en config + anidar `datos` en el análisis local | Sí (mínimo: `config.py`, `registrador.py`) |
| **2** | Concentrador: leer workers por SSE + enviar análisis a la nube (`nube.py`) | No |
| **3** | Concentrador: bajar config de la nube y repartirla a los workers | No |
| **4** | Cola offline + reintentos + heartbeat | No |
| **5** | Estadísticas locales en el concentrador (SQLite) | No |
| **6** | Front local unificado en el concentrador | No |
| **7** | Backend + frontend del sistema externo (nube) | No |
