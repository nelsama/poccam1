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

## Importante: la cámara NO se configura desde acá

El concentrador aplica configuración a los workers usando
`POST /api/externo/config`, que **ignora** `camara_fuente` y `fuente`. La URL de
la cámara es hardware local y solo se ajusta desde el front local (el panel del
concentrador, que lo reenvía al worker correspondiente).
