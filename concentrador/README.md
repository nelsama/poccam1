# Concentrador — motor (Fase 2)

Agrega varios **workers** (cámaras) y los comunica con el **sistema externo**
(nube). Es un proceso local, headless: no tiene interfaz todavía (el front
unificado viene en la Fase 6).

## Qué hace

1. **Recolecta** los análisis de cada worker (se suscribe a su SSE).
2. **Reenvía** cada análisis a la nube con el sobre genérico del contrato.
3. **Baja** la configuración de la nube y la reparte a cada worker.
4. **Encola** si no hay internet y reintenta (no se pierde nada).
5. **Reporta** el estado de cada worker (heartbeat).

## Módulos

| Archivo | Qué hace |
|---|---|
| `main.py` | Proceso principal: orquesta los hilos |
| `backend/clientes.py` | Habla con los workers (API + SSE) |
| `backend/nube.py` | Habla con el backend externo (contrato) |
| `backend/cola.py` | Cola offline de pendientes |
| `config.yaml` | Lista de workers + datos de la nube |

## Uso

```bash
# 1. Asegurate de tener el worker corriendo
python main.py

# 2. En otra terminal, arrancá el concentrador
python concentrador/main.py
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
la cámara es hardware local y solo se ajusta desde el panel del propio worker.
