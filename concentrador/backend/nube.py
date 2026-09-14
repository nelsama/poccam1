"""Cliente del backend externo (nube / Vercel).

Implementa el lado del concentrador del contrato definido en
`CONTRATO_NUBE.md`:

  • Enviar análisis/eventos   → POST /api/concentrador/analisis
  • Bajar configuración       → GET  /api/concentrador/config?version=N
  • Reportar estado           → POST /api/concentrador/estado
  • Publicar cambios locales  → POST /api/concentrador/config

Todas las peticiones las inicia el concentrador (conexiones salientes), así
que no requiere IP pública ni túneles.
"""

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger("concentrador")


class ClienteNube:
    """Cliente HTTP del sistema externo."""

    def __init__(self, base_url: str, token: str = "", timeout: int = 15,
                 concentrador_id: str = "concentrador-1"):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.concentrador_id = concentrador_id
        # Última versión de config conocida (para el GET condicional)
        self.version_config = 0

    # ── HTTP base ──────────────────────────────────────────────────

    def _cabeceras(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _get(self, ruta: str):
        """Devuelve (codigo, contenido_dict_o_None)."""
        req = urllib.request.Request(
            f"{self.base_url}{ruta}", headers=self._cabeceras(),
            method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, None
            logger.warning("GET %s → HTTP %s: %s", ruta, e.code,
                           e.read()[:200])
            return e.code, None

    def _post(self, ruta: str, cuerpo: dict) -> bool:
        datos = json.dumps(cuerpo, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{ruta}", data=datos,
            headers=self._cabeceras(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                r.read()
                return 200 <= r.status < 300
        except urllib.error.HTTPError as e:
            logger.warning("POST %s → HTTP %s: %s", ruta, e.code,
                           e.read()[:200])
            return False
        except (urllib.error.URLError, OSError) as e:
            logger.warning("POST %s falló (sin conexión?): %s", ruta, e)
            return False

    # ── Operaciones del contrato ───────────────────────────────────

    def enviar_analisis(self, sobre: dict) -> bool:
        """Envía UN análisis (sobre genérico) al sistema externo.

        `sobre` debe tener: tipo, worker_id, evento_id, timestamp, esquema,
        datos, meta.
        """
        return self._post("/api/concentrador/analisis", sobre)

    def bajar_config(self) -> dict | None:
        """Baja la configuración vigente para los workers.

        Usa GET condicional con `version`: si el servidor responde 304, no
        hay cambios (devuelve None). Si hay cambios, devuelve el objeto
        {"version": N, "workers": {...}} y actualiza la versión local.
        """
        codigo, datos = self._get(
            f"/api/concentrador/config?version={self.version_config}")
        if codigo == 304 or datos is None:
            return None
            
        version = datos.get("version")
        if isinstance(version, int):
            self.version_config = version
        return datos

    def reportar_estado(self, workers: dict) -> bool:
        """Reporta el estado de todos los workers (heartbeat)."""
        from datetime import datetime
        return self._post("/api/concentrador/estado", {
            "concentrador_id": self.concentrador_id,
            "timestamp": datetime.now().astimezone().isoformat(
                timespec="seconds"),
            "workers": workers,
        })

    def publicar_config(self, workers: dict) -> bool:
        """Publica en la nube cambios de config hechos desde el front local."""
        from datetime import datetime
        return self._post("/api/concentrador/config", {
            "concentrador_id": self.concentrador_id,
            "timestamp": datetime.now().astimezone().isoformat(
                timespec="seconds"),
            "workers": workers,
        })
