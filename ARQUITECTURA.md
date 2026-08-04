# Arquitectura del Backend Detector de Cambios

Documento técnico: cómo está construido el proyecto, cómo se comunican el
frontend y el backend, y cómo se extiende. Para la guía de uso ver `README.md`.

## 1. Visión general

**Monolito en un solo proceso Python.** No hay frontend separado: el panel web
es un HTML+JS embebido en `backend/web.py` que Flask sirve junto con una API
REST/SSE mínima.

Tres hilos conviven en el proceso:

```mermaid
flowchart LR
    M["Hilo principal: MonitorBackend (bucle)"] --> C["Capturador"]
    M --> D["DetectorCambios"]
    M --> R["RegistradorEventos"]
    W["Hilo web: Flask (daemon)"] --> C
    W --> D
    W --> CF["Config (compartida)"]
    J["Hilo MJPEG: consumidor del stream (daemon)"] --> C
```

- **Hilo principal**: el bucle `capturar → detectar → registrar`.
- **Hilo web** (daemon): Flask con el panel y la API.
- **Hilo del capturador MJPEG** (daemon): solo existe con fuentes HTTP MJPEG;
  consume el stream en segundo plano y conserva el frame más reciente.

> Los hilos comparten **el mismo objeto `DetectorCambios`**, **el mismo
> `Config`** y **el mismo capturador**. La web actualiza parámetros en caliente
> mutando esos objetos; el monitor los lee en cada ciclo. Las lecturas/escrituras
> simples de atributos son atómicas bajo el GIL de CPython, suficiente para este
> caso (no hay estados compuestos a proteger).

## 2. Módulos

| Módulo | Responsabilidad |
|---|---|
| `main.py` | `MonitorBackend`: bucle principal, logging, arranca la web en un hilo daemon, resumen al detener |
| `backend/config.py` | `Config` (dataclass): carga de `config.yaml`, serialización (`a_dict`/`guardar`) para persistir cambios desde la web |
| `backend/capturador.py` | Fuentes de imagen: `CapturadorPantalla` (mss), `CapturadorCamara` (OpenCV USB/RTSP con reconexión), `CapturadorMJPEG` (hilo + frame más reciente). Factory `crear_capturador(config)` |
| `backend/detector.py` | `DetectorCambios`: pipeline por frame (ROI → blur → alinear → diff/ssim/mse → umbral → área → estabilidad). Referencia estable. `actualizar()` para cambios en caliente |
| `backend/web.py` | Panel web (HTML+JS embebido) + API REST/SSE. Persistencia del ROI (`roi.json`) y notificador SSE de capturas |
| `backend/registrador.py` | `RegistradorEventos`: escribe `eventos.jsonl` (append), guarda imágenes en `capturas_cambio/`, aplica `max_imagenes`, notifica al panel (SSE) |

Dependencias entre módulos:

```mermaid
flowchart LR
    main --> capturador
    main --> config
    main --> detector
    main --> registrador
    main --> web
    detector --> web["web (cargar_roi)"]
    registrador --> web["web (notificar_captura_nueva)"]
    registrador --> config
```

> Nota de acoplamiento: `detector.py` y `registrador.py` importan funciones de
> `web.py` (`cargar_roi`, `notificar_captura_nueva`). Funciona porque `web.py`
> no importa esos módulos a nivel de módulo (los recibe por parámetro en
> `crear_app`). Ver "Deudas técnicas" §8.

## 3. El bucle del monitor (`main.py`)

```
while True:
    imagen = capturador.capturar()          # siempre el frame más reciente
    resultado = detector.procesar(imagen)   # pipeline completo
    if resultado["hubo_cambio"]:
        if pasó min_intervalo_eventos:
            registrador.registrar(evento)   # imagen + JSONL + aviso SSE
    esperar hasta completar intervalo_segundos
```

## 4. Flujo de datos

### Ruta de detección (por frame)

```
cámara → CapturadorMJPEG (hilo) → capturar() → DetectorCambios.procesar()
  → ROI (roi.json, recargado en cada llamada)
  → blur en color
  → alineación (si alinear_imagenes): correlación de fase + shift + margen
  → máscara de diferencia (ssim/diff/mse) con umbral de píxel
  → excluir borde + decidir por área (min_area_px)
  → filtro temporal (frames_estables)
  → RegistradorEventos.registrar()  (solo si cambio confirmado)
  → notificar_captura_nueva() → SSE → navegador
```

### Ruta de configuración (en vivo)

```
navegador → POST /api/config → valida → detector.actualizar(params)
  → muta Config compartida → config.guardar() → config.yaml
```

El monitor lee `config.intervalo_segundos` y `config.min_intervalo_eventos` en
cada ciclo; el detector usa sus propios atributos (actualizados por la web).
Por eso los cambios se aplican **sin reiniciar**.

### Ruta del ROI

```
canvas (mouse) → POST /api/roi → roi.json → el detector recarga el archivo
  en cada procesar() (cargar_roi) → se aplica al siguiente frame
```

### Ruta de la UI (sin polling)

```
GET /                → HTML+JS embebido (PAGINA)
GET /video           → MJPEG en vivo (con ROI dibujado sobre una COPIA del frame)
GET /api/ultimas     → últimas 2 capturas (bajo demanda al cargar)
GET /api/log         → eventos + sugerencias de ajuste (bajo demanda)
GET /api/eventos     → SSE: avisa cuando hay captura nueva (actualiza la UI)
GET /api/estado      → SSE: desplazamiento estimado + margen + interior/borde
GET/POST/DELETE /api/roi
GET/POST /api/config
DELETE /api/log      → limpia eventos.jsonl
GET /capturas/<nombre> → sirve una imagen (protegido contra rutas fuera de la carpeta)
```

## 5. Contrato de la API

| Endpoint | Método | Entrada | Salida |
|---|---|---|---|
| `/api/roi` | GET | — | `{"region": [x,y,w,h] \| null}` |
| `/api/roi` | POST | `{"region":[x,y,w,h]}` | `{"ok":true}` |
| `/api/roi` | DELETE | — | `{"ok":true}` |
| `/api/ultimas` | GET | — | `{"capturas":[{archivo,url,fecha},...]}` |
| `/api/log` | GET | — | `{"min_area_px":N, "eventos":[...]}` con sugerencias |
| `/api/log` | DELETE | — | `{"ok":true}` (vacía el JSONL) |
| `/api/config` | GET | — | config completa (`config.a_dict()`) |
| `/api/config` | POST | `{"captura":{...},"deteccion":{...}}` (parcial) | `{"ok":true}` o `400 {"error":...}` |
| `/api/eventos` | GET | — | SSE: `data: <contador>` cuando hay captura nueva |
| `/api/estado` | GET | — | SSE (cada ~1 s): `{"alinear":bool, "dy", "dx", "activo", "margen", "area_interior", "area_borde"}` |
| `/video` | GET | — | MJPEG multipart (`multipart/x-mixed-replace`) |
| `/capturas/<nombre>` | GET | — | PNG del evento |

El frontend usa `EventSource` para `/api/eventos` y `/api/estado` (notificación
push) y `fetch` para el resto. **No hay polling**: las capturas se actualizan
solo cuando el servidor avisa.

## 6. Persistencia

| Archivo | Escritor | Formato | Notas |
|---|---|---|---|
| `config.yaml` | web (`config.guardar`) | YAML | Se **regenera completo** al guardar desde la web (se pierden comentarios manuales) |
| `eventos.jsonl` | registrador | JSON por línea (append) | Toda la historia; la web lee las últimas 30 líneas |
| `capturas_cambio/` | registrador | PNG con timestamp en el nombre | Límite `max_imagenes` (elimina las más antiguas por orden de nombre) |
| `roi.json` | web | JSON | Se recarga en cada frame |

Todos ignorados por git (ver `.gitignore`).

## 7. Decisiones de diseño clave

1. **Capturador MJPEG con hilo propio**: OpenCV lee los streams MJPEG
   acumulando frames en un buffer interno (retraso de segundos). Este
   capturador consume el stream en un hilo y conserva **solo el último frame**,
   por lo que `capturar()` devuelve al instante el frame más fresco → la
   detección responde en milisegundos.
2. **El ROI se dibuja sobre una copia del frame** (`frame.copy()` en
   `generar_video`): dibujar el rectángulo sobre el frame compartido
   **contaminaba lo que analizaba el detector** (fue la causa histórica de
   falsos positivos de ~1150 px).
3. **Detección en color, no en gris**: un LED rojo sobre fondo oscuro puede
   tener el mismo valor de gris que el fondo; comparando los 3 canales no se
   pierde esa información.
4. **Referencia estable**: se compara contra la última imagen confirmada sin
   cambio (no contra el frame anterior). Se actualiza solo cuando no hay cambio
   o cuando un cambio se confirma — así un parpadeo no envenena la referencia.
5. **La vibración se compensa ANTES de comparar** (correlación de fase +
   shift), y el **borde** rellenado por el shift se **excluye** de la decisión
   (`margen`) y se reporta aparte (`area_borde`).
6. **`umbral` (0-1) = sensibilidad a nivel píxel** para ssim/diff (más bajo =
   más sensible) y corte de score para mse. `min_area_px` decide por área.
7. **Parámetros en vivo**: la web recibe el mismo objeto `Config` y
   `DetectorCambios` que el monitor; los muta y los persiste. El detector
   expone `actualizar()` para validar y aplicar cambios de detección.
8. **UI de un solo archivo**: `PAGINA` (string embebido) mantiene el proyecto
   autocontenido; a cambio, editar la UI requiere reiniciar el proceso.

## 8. Deudas técnicas / posibles refactors

- **`cargar_roi` vive en `web.py` pero lo usa el detector** → moverlo a un
  módulo compartido (p. ej. `backend/roi.py`) para invertir la dependencia.
- **Servidor de desarrollo de Flask** (`app.run`): suficiente para 1-2 clientes
  y 1 fps; para producción usar `waitress` (Windows) o `gunicorn` detrás de
  un proxy.
- **`PAGINA` embebida**: si el frontend crece, separarlo a `backend/static/` +
  `backend/templates/`.
- **Eventos en memoria** (`leer_log_eventos` lee el archivo completo): con
  millones de líneas convendrá un índice o base de datos ligera (SQLite).
- **Multi-cámara**: hoy es un proceso por cámara (puertos distintos). Para
  consolidar, refactorizar a un `MonitorBackend` por cámara dentro de un mismo
  proceso con `asyncio`/hilos y una tabla de cámaras.

## 9. Cómo extender

1. **IA bajo demanda**: en `main.py`, dentro del bloque `if hubo_cambio`,
   llamar al servicio de visión con `resultado["imagen_analizada"]` y guardar
   el resultado en el JSONL. Ya está previsto en la sección `ia:` del YAML.
2. **Verificación anti-oculsión** (pendiente de implementar): al confirmar un
   cambio, esperar ~1 s y recapturar; comparar el estado estabilizado contra la
   referencia anterior para descartar "algo que se cruzó" (el diseño está
   documentado en el README, sección Camino a producción).
3. **API externa**: agregar rutas FastAPI/Flask sobre los mismos objetos
   (consultar eventos, redefinir ROI, ajustar config).
4. **Shutter rápido en producción**: la vibración física durante la exposición
   causa desenfoque de movimiento que la alineación **no** puede corregir; se
   mitiga con exposición corta en la cámara y montaje amortiguado.
