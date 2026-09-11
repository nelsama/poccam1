# API — Detector de Cambios para Paneles Industriales

Documentación de la API HTTP del backend. Complementa a `README.md` (uso) y
`ARQUITECTURA.md` (diseño interno).

---

## 1. Convenciones

- **Base URL**: `http://<host>:5000` (por defecto `http://127.0.0.1:5000`).
  El servidor escucha en `0.0.0.0`, así que es accesible desde otro equipo de
  la red local con `http://<IP-del-servidor>:5000`.
- **Formato**: todas las respuestas son `application/json`, excepto `/video`
  (MJPEG) y `/capturas/<nombre>` (PNG), que son internos del panel.
- **Sesión de escritura**: `POST /api/config` acepta **cambios parciales**
  (solo los campos enviados). Los valores se aplican **en vivo** y se
  persisten en `config.yaml`.
- **Sin autenticación** (PoC). No exponer fuera de la red local.
- **La API pública NO expone imagen de cámara.** `/video` y `/capturas/*`
  existen para el panel web interno; no forman parte del contrato externo.

### Códigos de estado

| Código | Significado |
|---|---|
| `200` | Operación correcta |
| `400` | Valor inválido (falta un campo, tipo incorrecto, fuera de rango) |
| `502` | La cámara nueva no se pudo abrir (se mantuvo la anterior) |
| `503` | La configuración no está disponible |

---

## 2. Configuración

### `GET /api/config`

Devuelve todos los parámetros actuales **en vivo**, más datos de runtime.

**Respuesta** (ejemplo abreviado):

```json
{
  "captura": {
    "fuente": "camara",
    "camara_fuente": "http://172.26.10.201:8080/video",
    "nombre_camara": "panel-1",
    "region": null,
    "monitor": 1,
    "intervalo_segundos": 0.25,
    "aplicar_preset_al_iniciar": true,
    "rotacion": 90,
    "reconectar_segundos": 180.0
  },
  "deteccion": {
    "metodo": "ssim",
    "umbral": 0.5,
    "min_area_px": 350,
    "blur_ksize": 9,
    "marcar_cambios": false,
    "frames_estables": 2,
    "min_intervalo_eventos": 1.0,
    "alinear_imagenes": false,
    "max_desplazamiento": 30.0
  },
  "web": { "enabled": true, "host": "0.0.0.0", "port": 5000 },
  "registro": {
    "log_eventos": "eventos.jsonl",
    "save_changes": true,
    "output_dir": "capturas_cambio",
    "max_imagenes": 20,
    "nivel": "INFO"
  },
  "ia": {
    "enabled": false,
    "model": "deepseek-v4-flash-vision-exp",
    "detail": "low"
  },
  "presets": [ { "id": "vga", "nombre": "640x480 (VGA)", "ancho": 640, "alto": 480 }, ... ],
  "camara_resolucion": [640, 480],
  "preset_aplicado": "vga"
}
```

> **Nota de seguridad:** `ia.api_key` nunca se incluye (ni se escribe a
> `config.yaml`). La clave se lee de la variable de entorno
> `DEEPSEEK_API_KEY` o del archivo `ia.key`.

---

### `POST /api/config`

Aplica parámetros en vivo y los persiste. Cuerpo con secciones parciales:

```json
{
  "captura":   { "...": "..." },
  "deteccion": { "...": "..." }
}
```

#### Campos de `captura`

| Campo | Tipo | Valores válidos | Default | ¿Requiere recrear cámara? |
|---|---|---|---|---|
| `fuente` | string | `"camara"` \| `"pantalla"` | `"pantalla"` | Sí |
| `camara_fuente` | string | índice USB (`"0"`), URL HTTP/MJPEG, URL RTSP | `"0"` | **Sí (en vivo)** |
| `nombre_camara` | string | texto libre | `"camara"` | No |
| `intervalo_segundos` | float | `> 0` | `1.0` | No |
| `rotacion` | int | `0` \| `90` \| `180` \| `270` (horario) | `0` | No |
| `reconectar_segundos` | float | `>= 0` (`0` = nunca) | `180.0` | No |

> `aplicar_preset_al_iniciar`, `region` y `monitor` se configuran en
> `config.yaml` (no se exponen por API).

#### Campos de `deteccion`

| Campo | Tipo | Valores válidos | Default |
|---|---|---|---|
| `metodo` | string | `"ssim"` \| `"diff"` \| `"mse"` | `"ssim"` |
| `umbral` | float | `0.0`–`1.0` (más bajo = más sensible) | `0.5` |
| `min_area_px` | int | `>= 0` | `100` |
| `blur_ksize` | int | `>= 0` | `5` |
| `marcar_cambios` | bool | `true` \| `false` | `false` |
| `frames_estables` | int | `>= 1` | `2` |
| `min_intervalo_eventos` | float | `>= 0` | `5.0` |
| `alinear_imagenes` | bool | `true` \| `false` | `false` |
| `max_desplazamiento` | float | `>= 1.0` | `10.0` |

**Respuesta**: `{"ok": true}`

**Errores**:
- `400` con `{"ok": false, "error": "..."}` si un valor es inválido.
- `502` si se intentó cambiar `camara_fuente`/`fuente` y la cámara nueva no
  abrió. **La cámara anterior sigue funcionando.**

#### Ejemplo — cambiar la cámara en vivo

```bash
curl -X POST http://192.168.1.50:5000/api/config \
  -H "Content-Type: application/json" \
  -d '{
        "captura": {
          "fuente": "camara",
          "camara_fuente": "http://172.26.10.201:8080/video"
        }
      }'
```

Respuesta `{"ok": true}`: el capturador se recreó al instante, **sin reiniciar
el proceso**. Si la URL está mal, responde `502` y no se pierde la cámara actual.

#### Ejemplo — ajustar detección

```bash
curl -X POST http://192.168.1.50:5000/api/config \
  -H "Content-Type: application/json" \
  -d '{"deteccion": {"min_area_px": 700, "blur_ksize": 11}}'
```

---

### `GET /api/estado-sistema`

Estado actual **completo** (solo lectura): configuración + runtime.

```json
{
  "config": { "...": "igual que GET /api/config" },
  "runtime": {
    "camara_viva": true,
    "camara_resolucion": [640, 480],
    "preset_aplicado": "vga",
    "rotacion_efectiva": 90,
    "roi": [321, 173, 179, 48],
    "ia_activa": false,
    "capturas": 1234,
    "cambios": 12
  }
}
```

| Campo de `runtime` | Significado |
|---|---|
| `camara_viva` | `true` si hay frames disponibles ahora mismo |
| `camara_resolucion` | Resolución real del stream detectada al arrancar |
| `preset_aplicado` | Preset de parámetros aplicado según la resolución |
| `rotacion_efectiva` | Rotación en grados que se está aplicando |
| `roi` | Área de análisis `[left, top, width, height]` o `null` |
| `ia_activa` | Si el análisis IA está encendido en esta sesión |
| `capturas` | Total de capturas realizadas desde el arranque |
| `cambios` | Total de eventos detectados desde el arranque |

---

### `POST /api/rotacion`

Atajo para fijar solo la rotación.

```json
{ "rotacion": 90 }
```

**Respuesta**: `{"ok": true, "rotacion": 90}`

---

## 3. Área de análisis (ROI)

La ROI limita el análisis a un rectángulo (en píxeles de la imagen **ya
rotada**). Sin ROI se analiza el frame completo.

| Método | Ruta | Cuerpo | Respuesta |
|---|---|---|---|
| `GET` | `/api/roi` | — | `{"region": [x,y,w,h]}` o `{"region": null}` |
| `POST` | `/api/roi` | `{"region": [x,y,w,h]}` | `{"ok": true}` |
| `DELETE` | `/api/roi` | — | `{"ok": true}` |

```bash
curl -X POST http://127.0.0.1:5000/api/roi \
  -H "Content-Type: application/json" \
  -d '{"region": [321, 173, 179, 48]}'
```

---

## 4. Análisis IA

El análisis arranca **detenido** en cada arranque (`ia_enabled` se fuerza a
`false` en runtime, para no gastar). El toggle es **de sesión**.

| Método | Ruta | Cuerpo | Respuesta |
|---|---|---|---|
| `GET` | `/api/ia` | — | `{"enabled": bool, "model": "...", "detail": "..."}` |
| `POST` | `/api/ia` | `{"enabled": true\|false}` | `{"ok": true, "enabled": bool}` |
| `GET` | `/api/ia/prompt` | — | `{"prompt": "..."}` |
| `POST` | `/api/ia/prompt` | `{"prompt": "..."}` | `{"ok": true}` |
| `GET` | `/api/ia/prompts-hist` | — | `{"prompts": ["...", "..."]}` (máx 10) |
| `POST` | `/api/ia/prompts-hist` | `{"prompt": "..."}` (opcional) | `{"ok": true, "prompts": [...]}` |
| `POST` | `/api/ia/prompts-hist/delete` | `{"prompt": "..."}` | `{"ok": true, "prompts": [...]}` |

> El **prompt define la estructura del JSON de respuesta**. El backend no
> impone campos: solo agrega una instrucción neutra para que la IA responda
> en JSON válido respetando lo pedido.

---

## 5. Datos y resultados

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/api/ultimas` | Las 2 capturas más recientes: `{"capturas":[{"archivo","url","fecha"}]}` |
| `GET` | `/api/log` | Últimos eventos + `min_area_px` en vivo + sugerencia de ajuste |
| `DELETE` | `/api/log` | Vacía `eventos.jsonl` |
| `GET` | `/api/analisis` | Últimos 10 análisis IA (JSON completo que devolvió el modelo) |

---

## 6. SSE (Server-Sent Events)

Canales de notificación push. **No usar polling** para estos casos.

| Ruta | Cuándo emite | Contenido |
|---|---|---|
| `/api/eventos` | Se registra una captura nueva | `data: <contador>` |
| `/api/config-eventos` | Cambia la configuración (panel o API) | `data: <contador>` |
| `/api/estado` | Cada ~1 s si cambió | `{alinear, dy, dx, activo, margen, area_interior, area_borde}` |
| `/api/eventos-analisis` | Hay un análisis IA nuevo | `data: <contador>` |

```bash
# Escuchar cambios de configuración en vivo
curl -N http://127.0.0.1:5000/api/config-eventos
```

> En Postman estos requests quedan abiertos: cancelar manualmente. En el
> navegador se consume con `new EventSource('/api/config-eventos')`.

---

## 7. Panel interno (NO es API pública)

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/` | Panel web (HTML+JS): video, ROI, parámetros, IA |
| `GET` | `/video` | Stream MJPEG en vivo |
| `GET` | `/capturas/<nombre>` | PNG de un evento |

Estos endpoints sirven la **imagen de la cámara** y el panel; no forman parte
del contrato para clientes externos.

---

## 8. Flujo típico de un cliente externo

1. `GET /api/estado-sistema` → saber si la cámara está viva y la config actual.
2. `GET /api/config` → leer parámetros.
3. `POST /api/config` → inyectar parámetros (detección, captura o cámara).
4. `GET /api/config-eventos` (SSE) → enterarse de cambios hechos por otros
   clientes o por el panel web.
5. `GET /api/log` · `GET /api/analisis` → consumir resultados.
