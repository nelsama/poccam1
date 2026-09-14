"""Mock del backend externo (nube) para probar el concentrador localmente.

Simula los endpoints del contrato de `CONTRATO_NUBE.md` sin necesidad de
Vercel ni de base de datos. Sirve para verificar el flujo completo:

    concentrador ──► (este mock)
                 ◄──

Uso:
    python tests/mock_nube.py            # escucha en :7000

Endpoints:
    POST /api/concentrador/analisis   → recibe y guarda los análisis
    GET  /api/concentrador/config     → devuelve config (con version)
    POST /api/concentrador/estado     → recibe heartbeat
    POST /api/concentrador/config     → recibe config publicada
    GET  /recibidos                   → (extra) muestra lo recibido
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PUERTO = 7000

# "Base de datos" en memoria
RECIBIDOS = {"analisis": [], "estados": [], "configs": []}
# Config que el mock ofrece a los concentradores
CONFIG_ACTUAL = {
    "version": 1,
    "workers": {
        "panel-1": {
            "deteccion": {"min_area_px": 300},
        }
    },
}


class Handler(BaseHTTPRequestHandler):
    def _json(self, codigo: int, cuerpo: dict | None = None):
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if cuerpo is not None:
            self.wfile.write(json.dumps(cuerpo, ensure_ascii=False).encode())

    def _leer_cuerpo(self) -> dict:
        largo = int(self.headers.get("Content-Length", 0))
        if not largo:
            return {}
        try:
            return json.loads(self.rfile.read(largo).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # ── GET ────────────────────────────────────────────────────────
    def do_GET(self):
        if self.path.startswith("/api/concentrador/config"):
            version = CONFIG_ACTUAL["version"]
            if "version=" in self.path:
                try:
                    pedida = int(self.path.split("version=")[1].split("&")[0])
                except (ValueError, IndexError):
                    pedida = -1
                if pedida == version:
                    self.send_response(304)
                    self.end_headers()
                    print(f"  <- 304 (sin cambios, version {version})")
                    return
            self._json(200, CONFIG_ACTUAL)
            print(f"  <- 200 config version {version}")
            return

        if self.path == "/recibidos":
            self._json(200, RECIBIDOS)
            return

        self._json(404, {"error": "no encontrado"})

    # ── POST ───────────────────────────────────────────────────────
    def do_POST(self):
        cuerpo = self._leer_cuerpo()

        if self.path == "/api/concentrador/analisis":
            RECIBIDOS["analisis"].append(cuerpo)
            print(f"  <- ANALISIS recibido de {cuerpo.get('worker_id')} "
                  f"(evento {cuerpo.get('evento_id')}, "
                  f"esquema {cuerpo.get('esquema')})")
            print("     datos: "
                  + json.dumps(cuerpo.get("datos"), ensure_ascii=False))
            self._json(200, {"ok": True, "recibido": cuerpo.get("evento_id")})
            return

        if self.path == "/api/concentrador/estado":
            RECIBIDOS["estados"].append(cuerpo)
            print(f"  <- ESTADO recibido ({len(cuerpo.get('workers', {}))} workers)")
            self._json(200, {"ok": True})
            return

        if self.path == "/api/concentrador/config":
            RECIBIDOS["configs"].append(cuerpo)
            print("  <- CONFIG publicada por el concentrador")
            self._json(200, {"ok": True})
            return

        self._json(404, {"error": "no encontrado"})

    def log_message(self, formato, *args):
        pass  # silenciar el log por defecto de http.server


def main():
    puerto = int(sys.argv[1]) if len(sys.argv) > 1 else PUERTO
    servidor = HTTPServer(("127.0.0.1", puerto), Handler)
    print(f"Mock de la nube escuchando en http://127.0.0.1:{puerto}")
    print("  GET  /api/concentrador/config")
    print("  POST /api/concentrador/analisis")
    print("  POST /api/concentrador/estado")
    print("  GET  /recibidos")
    print("Ctrl+C para detener.\n")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nDetenido.")
        print(f"Total analisis recibidos: {len(RECIBIDOS['analisis'])}")


if __name__ == "__main__":
    main()
