# Backend Detector de Cambios para Paneles Industriales

Backend que pide imágenes a una cámara a intervalos configurables, compara
contra la imagen anterior, y **solo cuando detecta un cambio** guarda la imagen
y registra el evento en un log estructurado. Listo para crecer (API, envío a IA, etc.).

## 📁 Estructura

```
backend/
├── config.py        ← configuración (YAML)
├── capturador.py    ← fuente de imágenes: pantalla o cámara (USB/IP)
├── detector.py      ← detección de cambios (SSIM / diff / mse)
└── registrador.py   ← log JSONL + guardado de imágenes de eventos
main.py              ← bucle principal del backend
config.yaml          ← parámetros editables
eventos.jsonl        ← SE GENERA: log de eventos (1 línea JSON por evento)
capturas_cambio/     ← SE GENERA: imágenes de los eventos
```

## 🚀 Inicio Rápido

```bash
pip install -r requirements.txt

# Probar con la pantalla del computador (sin cámara)
python main.py

# Cámara USB (webcam)
python main.py --camara 0

# Cámara IP WiFi (RTSP)
python main.py --camara "rtsp://admin:clave@192.168.1.50:554/stream1"
```

## ⏱️ Parámetro clave: ¿cada cuánto pedir imagen a la cámara?

En `config.yaml`:

```yaml
captura:
  intervalo_segundos: 1.0   # ← segundos entre capturas
```

| Valor | Frecuencia | Cuándo usarlo |
|---|---|---|
| `0.5` | 2 fps | Cambios rápidos / proceso agitado |
| `1.0` | 1 fps | **Estándar para paneles industriales** |
| `3.0` | 0.33 fps | Cambios lentos (temperaturas, niveles) |
| `5.0` | 0.2 fps | Mínimo consumo / batería |

También se puede pasar por línea de comandos:

```bash
python main.py --intervalo 2.5        # sobreescribir el sondeo
python main.py --intervalo 0.5 --umbral 0.02   # rápido + sensible
```

> El backend SIEMPRE está pidiendo imágenes a la cámara (modelo de sondeo).
> Pero solo escribe en el log y guarda la imagen cuando detecta un cambio.

## 📋 El log de eventos (`eventos.jsonl`)

Un evento = una línea JSON. Ejemplo:

```json
{"evento_id": "20260802_163957_153", "timestamp": "2026-08-02T16:39:57.164",
 "camara": "rtsp://192.168.1.50:554/stream1", "metodo": "ssim",
 "score": 0.019306, "area_px": 78951,
 "imagen_original": "capturas_cambio\\evento_..._original.png",
 "imagen_marcada": "capturas_cambio\\evento_..._marcado.png",
 "capturas_total": 2}
```

Cada línea es JSON válido → fácil de procesar después con pandas, jq, o Power BI:

```bash
# Contar eventos con jq (Linux)
cat eventos.jsonl | jq -s 'length'

# Leer en Python para estadísticas
import pandas as pd
df = pd.read_json("eventos.jsonl", lines=True)
df["timestamp"] = pd.to_datetime(df["timestamp"])
```

## 📷 Imágenes por evento

Cada cambio guarda **2 archivos** en `capturas_cambio/`:

- `evento_<fecha>_original.png` → la imagen tal cual (la que enviarías a una IA)
- `evento_<fecha>_marcado.png` → la misma con los cambios resaltados en rojo

## ⚙️ Configuración completa (`config.yaml`)

| Sección | Parámetro | Default | Descripción |
|---|---|---|---|
| `captura` | `fuente` | `pantalla` | `pantalla` o `camara` |
| `captura` | `camara_fuente` | `0` | índice USB o URL RTSP |
| `captura` | `region` | `null` | `[left, top, width, height]` para solo una zona |
| `captura` | `intervalo_segundos` | `1.0` | **cada cuánto pedir imagen** |
| `deteccion` | `metodo` | `ssim` | `ssim` (recomendado) / `diff` / `mse` |
| `deteccion` | `umbral` | `0.05` | sensibilidad (bajo = más sensible) |
| `deteccion` | `min_area_px` | `50` | ignora cambios de área menor |
| `registro` | `log_eventos` | `eventos.jsonl` | ruta del log |
| `registro` | `output_dir` | `capturas_cambio` | carpeta de imágenes |

## 🔧 Calibración del umbral

1. Apunta al panel estático y ejecuta con `--umbral 0.02`
2. Sin mover nada: si registra "cambios" falsos → **sube** el umbral (0.05, 0.1)
3. Haz un cambio real (cambia un valor): si NO registra → **baja** el umbral
4. El punto medio es tu configuración

## 🏭 Camino a producción

1. **Hoy**: pantalla o webcam → valida la lógica
2. **Próximo paso**: cámara IP WiFi (RTSP) en `camara_fuente`
3. **Crecimiento natural** (cuando lo necesites):
   - FastAPI con `/api/eventos` para consultar el historial
   - Envío de la imagen a una IA solo cuando `hubo_cambio` (ahorro ~99%)
   - Multi-cámara con `asyncio` (un proceso por cámara)
   - Docker + systemd para arranque automático
