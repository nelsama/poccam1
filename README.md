# Backend Detector de Cambios para Paneles Industriales

Backend que pide imágenes a una cámara a intervalos configurables, compara contra
una **referencia estable** y, **solo cuando detecta un cambio real**, guarda la
imagen y registra el evento en un log estructurado.

Diseñado para **paneles industriales genéricos** (display de 7 segmentos, LCD,
matrices, medidores, fondos de cualquier color): no hay nada específico de un
display en particular.

> 💡 **Por qué existe**: en producción, la idea es enviar la imagen a una IA de
> visión **solo cuando hay un cambio** (no periódicamente). Esto ahorra ~99% del
> costo de análisis. Este backend es la "alarma" que decide cuándo disparar.

## 📚 Documentación

| Documento | Contenido |
|---|---|
| **`README.md`** (este) | Qué hace, cómo usarlo, pipeline, parámetros y calibración |
| **[`API.md`](API.md)** | **Referencia de la API HTTP**: endpoints, campos, ejemplos y SSE |
| **[`ARQUITECTURA.md`](ARQUITECTURA.md)** | Diseño interno: módulos, hilos, decisiones y deudas técnicas |
| **[`CONTRATO_NUBE.md`](CONTRATO_NUBE.md)** | Diseño del concentrador y su contrato con el sistema externo (nube) |
| **`postman_collection.json`** | Colección de Postman lista para importar (30 requests) |

La API de configuración/estado (solo JSON) es consumible desde otro equipo de
la red y **no expone la imagen de cámara**. Ver `API.md` para el contrato.

## ✨ Qué incluye

- **Panel web** (`http://localhost:5000`) para:
  - Ver el video en vivo y **definir el área de análisis (ROI)** con el mouse
  - Editar **todos los parámetros en tiempo real** (sin reiniciar el servicio)
  - Ver las últimas capturas y el log de eventos con sugerencias de ajuste
  - Indicador en vivo de la compensación de vibración
- **Compensación de vibración** (registro de imágenes por correlación de fase)
- Detección en **color** (no en gris: un LED rojo sobre fondo oscuro se detecta
  aunque su gris sea igual al fondo)
- Filtros anti-falsos-positivos: estabilidad temporal, área mínima, exclusión
  del borde de la alineación

## 📁 Estructura (monorepo)

```
worker/                ← WORKER: captura, detección, IA (corre junto a la cámara)
├── main.py            ← bucle principal
├── config.yaml        ← parámetros editables (se regenera al guardar desde la web)
├── backend/
│   ├── config.py      ← configuración (dataclass + YAML, guardado en vivo)
│   ├── capturador.py  ← fuente de imágenes: pantalla, cámara USB/IP, MJPEG/RTSP
│   ├── detector.py    ← pipeline de detección (ROI, vibración, diff, filtros)
│   ├── ia.py          ← análisis por visión (DeepSeek) + prompt editable
│   ├── web.py         ← panel web + API (ROI, parámetros en vivo, log, IA)
│   └── registrador.py ← log JSONL + guardado de imágenes y análisis
├── eventos.jsonl      ← SE GENERA: log de eventos
├── capturas_cambio/   ← SE GENERA: imágenes de los eventos
├── analisis_ia/       ← SE GENERA: JSON de los análisis de IA
└── roi.json           ← SE GENERA: el área de análisis

concentrador/          ← CONCENTRADOR: agrega varios workers y habla con la nube
├── main.py
├── config.yaml        ← lista de workers + datos de la nube
└── backend/{clientes,nube,cola}.py

tests/                 ← mock de la nube + test de integración del concentrador

backend-nube/          ← (futuro) API en Vercel + Supabase

README.md · API.md · ARQUITECTURA.md · CONTRATO_NUBE.md · postman_collection.json
```

## 🚀 Inicio Rápido

```bash
pip install -r requirements.txt

# El worker se ejecuta desde su carpeta:
cd worker

# Cámara IP (URL HTTP MJPEG o RTSP):
python main.py --camara "http://192.168.1.144:8080/stream/frontal"
python main.py --camara "rtsp://admin:clave@192.168.1.50:554/stream1"
python main.py --camara 0                      # webcam USB

# Sin cámara: probar capturando la pantalla del computador
python main.py
```

Al arrancar imprime la configuración y deja el panel web en
`http://localhost:5000` (accesible desde otros equipos de la red).

> Si la cámara no responde al arrancar (apagada, sin red, stream no
> iniciado), el backend lo avisa y **reintenta cada 5 s** hasta que
> vuelve a estar disponible — no termina con un error. Ctrl+C para salir.

## 🌐 Panel web

Abre `http://localhost:5000` (o `http://IP-del-equipo:5000`).

| Zona | Qué hace |
|---|---|
| **Video en vivo** | Arrastra el mouse para dibujar el **rectángulo de análisis (ROI)**. Solo lo que está dentro se analiza: el fondo (personas, luces, movimiento) se ignora por completo. Botones 💾 Guardar área / 🗑️ Quitar área |
| **Indicador de vibración** | Muestra el desplazamiento estimado en vivo: `✅ Sin vibración (dy, dx)` / `⚠️ Vibración detectada` / `🚫 Compensación DESACTIVADA`. Incluye el margen de borde y el desglose interior/borde de la última comparación |
| **⚙️ Parámetros (en vivo)** | Todos los parámetros de captura y detección. Cada campo se aplica solo al terminar de editarlo (Enter o clic fuera) y **se guarda en `config.yaml`** — sin reiniciar. Select **📷 Preset**: configuración de partida según la resolución de la cámara (VGA, HD, FullHD, 4 MP, 5 MP, 4K) que ajusta `min_area_px`, `blur` y `max_desplazamiento`. Al iniciar, el backend **detecta la resolución del stream** y aplica el preset que le corresponde (lo verás seleccionado en el select, con mensaje). Botones: 💾 Aplicar todo y 🔄 Recargar valores (re-sincroniza con el servidor) |
| **📸 Últimas capturas** | Las 2 imágenes de eventos más recientes con su fecha (se actualizan solo cuando hay una nueva) |
| **📋 Log de cambios** | Cada evento con su área de píxeles, score y una **sugerencia de ajuste** (p. ej. "sube min_area_px a X"). Botón para limpiar |

**Parámetros condicionales**: el panel deshabilita (atenúa) los parámetros que
no aplican en el modo actual — p. ej. `min_area_px` no aplica con el método
`mse`, y `max_desplazamiento` solo aplica con la compensación de vibración
activa.

## ⚙️ Cómo funciona el pipeline (por frame)

Cada ciclo de captura procesa la imagen así:

```mermaid
flowchart TD
    A["1. Capturar frame"] --> B["2. Recortar ROI (solo el área definida)"]
    B --> C["3. Desenfocar en color (blur_ksize)"]
    C --> D["4. Compensar vibración (si alinear_imagenes)"]
    D --> E["5. Diferencia vs referencia (ssim / diff / mse)"]
    E --> F["6. Máscara con umbral de píxel (umbral 0-1)"]
    F --> G["7. Excluir borde de alineación + área mínima (min_area_px)"]
    G --> H["8. Filtro temporal (frames_estables)"]
    H --> I{"¿Cambio confirmado?"}
    I -- Sí --> J["Guardar imagen + evento + aviso SSE al panel"]
    I -- No --> A
```

Detalle de cada etapa:

1. **Capturar** — el capturador MJPEG mantiene un hilo de fondo leyendo el
   stream y conserva **solo el frame más reciente** (latencia mínima; OpenCV
   por sí solo acumula frames viejos y retrasa la detección).
2. **ROI** — se descarta todo lo que esté fuera del rectángulo definido en la
   web. El detector **nunca ve** el fondo.
3. **Desenfoque** — elimina ruido del sensor y de la compresión JPEG.
   Se aplica por igual a la referencia y al frame actual.
4. **Compensación de vibración** *(opcional)* — antes de comparar, la
   correlación de fase estima cuánto se movió la cámara (X/Y, sub-píxel) y
   desplaza el frame actual **para cuadrarlo con la referencia**. Así la
   vibración no aparece como cambio. Devuelve también un **margen** (el borde
   que quedó "inventado" por el relleno), que la siguiente etapa descarta.
5. **Diferencia** — según el método:
   - `ssim` *(recomendado)*: métrica estructural, robusta a luz/sombras/ruido
   - `diff`: resta píxel a píxel en color (útil para debug)
   - `mse`: error cuadrático medio (no usa área; no recomendado)
6. **Umbral de píxel** — `umbral` (0-1) define cuán distinto debe ser un píxel
   para contarse como cambiado. **Más bajo = más sensible**. En `ssim`, 0.5
   equivale al corte clásico.
7. **Área** — se excluye la franja del borde de la alineación (ahí el shift
   rellena píxeles que no son reales) y se exige que el área de cambio supere
   `min_area_px`. El área de borde se reporta aparte (`area_borde`).
8. **Estabilidad** — el cambio debe persistir `frames_estables` capturas
   consecutivas para confirmarse (un parpadeo o ruido momentáneo no pasa).

**Referencia**: el detector compara contra la **última imagen estable** (no
contra el frame anterior). Se actualiza cuando no hay cambio y cuando un cambio
se confirma; **no** se actualiza durante un cambio pendiente de confirmar.

## 🎛️ Parámetros (`config.yaml`)

Todos se pueden editar en vivo desde el panel web (se aplican y se guardan solos).

### Captura

| Parámetro | Default | Descripción |
|---|---|---|
| `fuente` | `pantalla` | `pantalla` o `camara` |
| `camara_fuente` | `0` | índice USB, URL RTSP o URL HTTP MJPEG |
| `nombre_camara` | `camara` | nombre legible que queda en el log de eventos |
| `intervalo_segundos` | `1.0` | **cada cuánto pedir imagen a la cámara** (0.25 = 4 fps de prueba) |
| `aplicar_preset_al_iniciar` | `true` | al iniciar, detecta la resolución real del stream y aplica el preset correspondiente |
| `region` / `monitor` | — | solo para captura de pantalla |

### Detección

| Parámetro | Default | Descripción |
|---|---|---|
| `metodo` | `ssim` | `ssim` (recomendado) / `diff` / `mse` |
| `umbral` | `0.5` | sensibilidad a nivel píxel (0-1). **Más bajo = más sensible**. Aplica a los 3 métodos |
| `min_area_px` | `100` | área mínima de cambio para disparar (no aplica con `mse`) |
| `blur_ksize` | `5` | desenfoque (impar). Elimina ruido de compresión |
| `frames_estables` | `2` | capturas consecutivas para confirmar un cambio (anti-parpadeo) |
| `min_intervalo_eventos` | `5.0` | segundos mínimos entre eventos (freno anti-spam) |
| `marcar_cambios` | `false` | guarda además una imagen con los cambios resaltados en rojo (debug) |

### Compensación de vibración (registro de imágenes)

| Parámetro | Default | Descripción |
|---|---|---|
| `alinear_imagenes` | `false` | activa la alineación de imágenes antes de comparar |
| `max_desplazamiento` | `10.0` | límite de desplazamiento (px) a corregir. Por encima no corrige: es un cambio real de escena |

- La vibración típica de máquinas es 1–10 px → con 10–20 px estás cubierto.
- **No corrige desenfoque por movimiento**: si la cámara se mueve durante la
  exposición, los segmentos se ven más anchos (es un efecto físico del
  sensor). La solución real es **shutter rápido** en la cámara + montaje con
  amortiguación.
- La franja del borde que deja el relleno del desplazamiento se **excluye
  automáticamente** de la decisión y se reporta como `area_borde`.

### Registro

| Parámetro | Default | Descripción |
|---|---|---|
| `log_eventos` | `eventos.jsonl` | log estructurado (1 línea JSON por evento) |
| `save_changes` | `true` | guardar la imagen de cada evento |
| `output_dir` | `capturas_cambio` | carpeta de imágenes |
| `max_imagenes` | `20` | máximo de archivos en la carpeta (0 = sin límite) |
| `nivel` | `INFO` | `DEBUG` para ver todas las comparaciones |

> ⚠️ Al guardar parámetros desde la web, `config.yaml` se regenera completo
> (pierde los comentarios escritos a mano, pero el formato se conserva).

## 📋 El log de eventos (`eventos.jsonl`)

Un evento = una línea JSON. Ejemplo:

```json
{"evento_id": "20260803_123456_789", "timestamp": "2026-08-03T12:34:56.789",
 "camara": "panel-1", "metodo": "ssim", "score": 0.0193,
 "area_px": 547, "area_borde": 0,
 "imagen_original": "capturas_cambio\\evento_..._original.png",
 "imagen_marcada": "", "capturas_total": 12}
```

- `area_px` es el área de cambio **del contenido real** (interior).
- `area_borde` es la parte del área que cayó en la franja de la alineación
  (artefacto del shift) — si es grande, el evento pudo ser causado por
  vibración, no por el panel.

Procesar después con pandas / jq / Power BI:

```python
import pandas as pd
df = pd.read_json("eventos.jsonl", lines=True)
df["timestamp"] = pd.to_datetime(df["timestamp"])
```

## 📷 Imágenes por evento

- Siempre: `evento_<fecha>_original.png` → la imagen tal cual (la que enviarías a la IA)
- Con `marcar_cambios: true`: además `evento_<fecha>_marcado.png` → con contornos rojos sobre los cambios

`max_imagenes` mantiene la carpeta acotada (con `marcar_cambios` cada evento
ocupa 2 archivos, así el límite equivale a la mitad de eventos).

## 🔧 Calibración (desde el panel web)

1. **Define el ROI**: dibuja el rectángulo sobre el display. Si el área queda
   chica o mal ubicada, cambios reales no se detectarán (y viceversa).
2. **Ajusta `min_area_px`**: mira el log — cada evento reporta su área. Si ves
   eventos que no deberían existir, súbelo; si un cambio real no dispara, bájalo.
3. **Ajusta `umbral`** (sensibilidad a nivel píxel): déjalo en 0.5 para empezar;
   bájalo si los cambios sutiles no se detectan, súbelo si el ruido cuenta
   demasiados píxeles.
4. **`frames_estables`**: 2 es un buen punto medio (0.5 s de confirmación a 4 fps).
5. **Vibración**: si la cámara está en una máquina que vibra, activa
   `alinear_imagenes` y observa el indicador del panel — debe mostrar el
   desplazamiento estimado y el desglose interior/borde.
6. **Método `diff`** para debug visual (activa también `marcar_cambios` para ver
   los contornos rojos exactos de lo que cambió).

## 🏭 Camino a producción

1. **Hoy**: cámara IP de prueba → validar lógica y calibración
2. **Próximo paso**: cámara industrial fija apuntando al panel, con **shutter
   rápido** para minimizar el desenfoque por vibración
3. **Crecimiento natural** (cuando lo necesites):
   - Envío de la imagen a una IA **solo cuando `hubo_cambio`** (ahorro ~99%)
   - Verificación anti-oculsión: al confirmar un cambio, esperar ~1 s y
     recapturar para descartar "algo que se cruzó" (diseño consultado, pendiente
     de implementar)
   - FastAPI con `/api/eventos` para consultar el historial
   - Multi-cámara (un proceso por cámara) + Docker / systemd
