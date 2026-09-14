"""Test de integración del concentrador (sin cámaras reales).

Levanta:
  • Un WORKER FALSO (HTTP) que simula la API del worker.
  • El MOCK DE LA NUBE (importado del otro archivo).
Y verifica que el concentrador:
  1. Detecta el análisis nuevo del worker (vía el flujo de recolección).
  2. Lo envía a la nube con el sobre genérico correcto.
  3. Baja config de la nube y la aplica al worker.

Uso:
    python tests/test_concentrador.py
"""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from concentrador.backend.clientes import Worker      # noqa: E402
from concentrador.backend.cola import ColaPendientes  # noqa: E402
from concentrador.backend.nube import ClienteNube     # noqa: E402

PUERTO_WORKER = 7501
PUERTO_NUBE = 7502

# ── WORKER FALSO ───────────────────────────────────────────────────
WORKER_ANALISIS = []
WORKER_CONFIG_APLICADA = []


class WorkerFalso(BaseHTTPRequestHandler):
    def _json(self, codigo, cuerpo):
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(cuerpo).encode())

    def do_GET(self):
        if self.path == "/api/analisis":
            self._json(200, {"analisis": list(WORKER_ANALISIS)})
        elif self.path == "/api/config":
            self._json(200, {"captura": {"worker_id": "panel-1"}})
        elif self.path == "/api/estado-sistema":
            self._json(200, {"config": {}, "runtime": {
                "camara_viva": True, "camara_resolucion": [640, 480],
                "ia_activa": True, "capturas": 100, "cambios": 3}})
        else:
            self._json(404, {})

    def do_POST(self):
        largo = int(self.headers.get("Content-Length", 0))
        cuerpo = json.loads(self.rfile.read(largo).decode()) if largo else {}
        if self.path == "/api/externo/config":
            WORKER_CONFIG_APLICADA.append(cuerpo)
            self._json(200, {"ok": True})
        else:
            self._json(404, {})

    def log_message(self, *a):
        pass


# ── Test ───────────────────────────────────────────────────────────
def main():
    # Arrancar worker falso y mock de nube
    from tests.mock_nube import Handler as HandlerNube
    hw = HTTPServer(("127.0.0.1", PUERTO_WORKER), WorkerFalso)
    hn = HTTPServer(("127.0.0.1", PUERTO_NUBE), HandlerNube)
    threading.Thread(target=hw.serve_forever, daemon=True).start()
    threading.Thread(target=hn.serve_forever, daemon=True).start()
    time.sleep(0.5)
    print("Worker falso en :%d  |  Nube mock en :%d\n" %
          (PUERTO_WORKER, PUERTO_NUBE))

    worker = Worker("panel-1", f"http://127.0.0.1:{PUERTO_WORKER}")
    nube = ClienteNube(f"http://127.0.0.1:{PUERTO_NUBE}", token="",
                       concentrador_id="test-1")

    # ── 1. Worker vivo ──
    est = worker.estado()
    assert est is not None and est["runtime"]["camara_viva"] is True
    print("1. OK: el concentrador ve el worker vivo")

    # ── 2. Enviar un análisis a la nube (sobre genérico) ──
    sobre = {
        "tipo": "analisis_ia",
        "worker_id": "panel-1",
        "evento_id": "20260910_143022_123",
        "timestamp": "2026-09-10T14:30:22.123-04:00",
        "esquema": "personas_v1",
        "datos": {"hay_persona": True, "confianza": 0.92},
        "meta": {"modelo": "deepseek-v4-flash-vision-exp", "area_px": 547},
    }
    ok = nube.enviar_analisis(sobre)
    assert ok, "falló el envío del análisis"
    print("2. OK: análisis enviado a la nube")

    # ── 3. La nube lo recibió con los campos correctos ──
    import urllib.request
    recibidos = json.loads(urllib.request.urlopen(
        f"http://127.0.0.1:{PUERTO_NUBE}/recibidos", timeout=5).read())
    assert len(recibidos["analisis"]) == 1
    r = recibidos["analisis"][0]
    assert r["worker_id"] == "panel-1"
    assert r["esquema"] == "personas_v1"
    assert r["datos"]["hay_persona"] is True
    print("3. OK: la nube recibió worker_id, esquema y datos correctos")

    # ── 4. Bajar config (primera vez: hay cambios) ──
    cfg = nube.bajar_config()
    assert cfg is not None and cfg["version"] == 1
    print("4. OK: config bajada (version 1)")

    # ── 5. Bajar config otra vez: 304, sin cambios ──
    cfg2 = nube.bajar_config()
    assert cfg2 is None, "debería ser None (304 sin cambios)"
    print("5. OK: segunda consulta devuelve 304 (sin cambios)")

    # ── 6. Aplicar config al worker ──
    for wid, bloques in cfg["workers"].items():
        ok = worker.aplicar_config(bloques)
        assert ok
    assert len(WORKER_CONFIG_APLICADA) == 1
    assert WORKER_CONFIG_APLICADA[0]["deteccion"]["min_area_px"] == 300
    print("6. OK: config aplicada al worker")

    # ── 7. Reportar estado (heartbeat) ──
    assert nube.reportar_estado({"panel-1": {"camara_viva": True}})
    recibidos = json.loads(urllib.request.urlopen(
        f"http://127.0.0.1:{PUERTO_NUBE}/recibidos", timeout=5).read())
    assert len(recibidos["estados"]) == 1
    print("7. OK: heartbeat reportado")

    # ── 8. Cola offline ──
    cola = ColaPendientes(Path(__file__).parent / "_test_cola.jsonl")
    cola.encolar({"evento_id": "pendiente-1"})
    cola.encolar({"evento_id": "pendiente-2"})
    assert cola.cantidad() == 2
    # Simular que la nube falla: no se envía nada
    enviados = cola.reenviar(lambda s: False)
    assert enviados == 0 and cola.cantidad() == 2
    # Ahora la nube funciona
    enviados = cola.reenviar(lambda s: True)
    assert enviados == 2 and cola.cantidad() == 0
    print("8. OK: cola offline encola y reenvía al recuperar conexión")
    # limpiar
    try:
        (Path(__file__).parent / "_test_cola.jsonl").unlink()
    except OSError:
        pass

    print("\n*** TODAS LAS PRUEBAS DEL CONCENTRADOR PASARON ***")


if __name__ == "__main__":
    main()
