# Concentrador — motor + panel unificado

Agrega varios **workers** (cámaras) y los comunica con el **sistema externo**
(nube). Además sirve el **panel web unificado**: un solo punto de entrada con
un selector para elegir qué cámara ver.

## Qué hace

1. **Recolecta** los análisis de cada worker (se suscribe a su SSE).
2. **Reenvía** cada análisis a la nube con el sobre genérico del contrato.
3. **Baja** la configuración de la nube y la reparte a cada worker.
4. **Encola** si no hay internet y reintenta (no se pierde nada).
5. **Reporta** el estado de cada worker (heartbeat).
6. **Sirve el panel web** en `http://localhost:8080`, con proxy hacia el worker
   activo.

## Módulos

| Archivo | Qué hace |
|---|---|
| `main.py` | Proceso principal: orquesta los hilos y arranca el panel |
| `backend/clientes.py` | Habla con los workers (API + SSE) |
| `backend/nube.py` | Habla con el backend externo (contrato) |
| `backend/cola.py` | Cola offline de pendientes |
| `backend/panel.py` | **Panel unificado**: sirve el HTML y hace proxy al worker activo |
| `config.yaml` | Lista de workers + panel + datos de la nube |

## Panel web unificado

```
NAVEGADOR ──▶ CONCENTRADOR :8080 ──▶ WORKER activo :5000
              (panel + proxy)          (API + video)
```

- El panel (HTML/JS) vive en **`compartido/panel.html`**, un único archivo que
  sirven tanto el worker como el concentrador — sin copias duplicadas.
- El JS usa **rutas relativas** (`fetch('/api/config')`, `imagen.src = '/video'`),
  así que el proxy del concentrador reenvía todo al worker activo sin cambios.
- El **selector** de arriba cambia el worker activo; al cambiarlo el panel se
  recarga y todas las vistas apuntan a la cámara nueva.
- La lista de workers sale del `config.yaml` (mapa `id → url`).

### Rutas propias del panel (no existen en el worker)

| Ruta | Qué hace |
|---|---|
| `GET /api/workers-panel` | Lista los workers y cuál está activo |
| `POST /api/worker-activo` | Cambia el worker activo (`{"id": "panel-1"}`) |

### Detalles del proxy que conviene conocer

- El **video** y los **SSE** se reenvían por trozos con `read1()`, sin
  bufferizar: acumular bytes introduciría retraso en los eventos.
- El `Content-Type` de los streams se reenvía **completo**, incluido el
  `boundary` del MJPEG. Sin él el navegador no muestra el video.
- Los **errores del worker** (4xx/5xx) se propagan tal cual, no se convierten
  en 200.

## Uso

```bash
# 1. Asegurate de tener el worker corriendo (desde su carpeta)
cd worker && python main.py

# 2. En otra terminal, arrancá el concentrador
cd concentrador && python main.py

# 3. Abrí el panel
#    http://localhost:8080
```

## Probar sin la nube

Hay un mock del backend externo para verificar el flujo completo localmente:

```bash
# Terminal A: mock de la nube (escucha en :7000)
python tests/mock_nube.py

# Terminal B: el concentrador (con la nube apuntando al mock)
python concentrador/main.py
```

En `concentrador/config.yaml`:
```yaml
nube:
  enabled: true
  base_url: "http://127.0.0.1:7000"
```

## Test de integración

Verifica el flujo completo sin cámaras reales (worker falso + nube mock):

```bash
python tests/test_concentrador.py
```

## Integración con Weizhou (estado de las máquinas)

Informa en tiempo real si cada máquina está **EN USO** o **LIBRE**, a partir de
lo que la IA detecta en el panel de la cámara.

```
Worker → Concentrador ──┬──▶ Weizhou   (estado en vivo → tablets)
                        └──▶ Vercel    (histórico en Supabase)
```

### Configuración (`config.yaml`)

```yaml
weizhou:
  enabled: true
  base_url: "https://weizhou.vercel.app"
  api_key: ""              # se lee de weizhou.key
  mapeo:
    panel-1:               # worker local → Lavadora 1
      maquina_id: "5b169038-16d9-4ca0-a9e8-a776c6d2b966"
      camara_id: "95369cfc-8136-45e9-8ea8-27d8085abbd3"
```

La clave se resuelve: entorno `WEIZHOU_API_KEY` > `weizhou.key` > `api_key`.

El `mapeo` traduce el **`worker_id` local** a los identificadores de Weizhou. Si
un worker no está en el mapeo, se envía su `worker_id` como `codigo` (requiere
que Weizhou tenga ese código externo asignado a una máquina).

### Máquinas disponibles

| `worker_id` | `maquina_id` | `camara_id` | Máquina |
|---|---|---|---|
| `panel-1` | `5b169038-…` | `95369cfc-…` | Lavadora 1 |
| — | `1d89e671-…` | `1b1d1946-…` | Lavadora 2 |
| — | `ecbb643d-…` | `5c747fea-…` | Secadora 1 |
| — | `40a1c6fb-…` | `a68db92d-…` | Planchadora / Dobladora 1 |

(Los UUID completos están en `config.yaml`.)

### Solo transiciones

La API de Weizhou pide explícitamente enviar **solo cuando el estado cambia**,
no en cada análisis. Si la máquina lleva 10 minutos en uso y la IA responde
`en_uso: true` en cada análisis, se envía **una sola vez**:

```
en_uso=true   → ENVIAR (inicio)
en_uso=true   → omitir
en_uso=true   → omitir
en_uso=false  → ENVIAR (fin)
en_uso=false  → omitir
```

El último estado enviado se guarda en `estado_weizhou.json`, así un reinicio del
concentrador no dispara un envío redundante.

### El esquema que debe producir la IA

El worker necesita el prompt con el esquema `estado_equipo_v1`
(`worker/ia_prompt.txt`):

```json
{ "en_uso": false, "confianza": 0.0, "notas": "" }
```

Además de `en_uso`, el JSON completo que devuelve la IA viaja en el campo
`payload` de la petición, para trazabilidad del lado de Weizhou.

### Probar la integración

```bash
# Lógica de transiciones (sin llamar a la API)
python tests/test_weizhou.py
```

### Diagnóstico

| Respuesta | Significa |
|---|---|
| `200` con `nombre` de la máquina | ✅ Enviado |
| `401` | La `x-api-key` es incorrecta |
| `404` | La máquina (`maquina_id`/`codigo`) no existe en Weizhou |
| `400` | Falta un campo obligatorio o `estado` inválido |

## Importante: la cámara NO se configura desde acá

El concentrador aplica configuración a los workers usando
`POST /api/externo/config`, que **ignora** `camara_fuente` y `fuente`. La URL de
la cámara es hardware local y solo se ajusta desde el front local (el panel del
concentrador, que lo reenvía al worker correspondiente).
