"""Configuración del backend de monitoreo de cámaras."""

from dataclasses import dataclass
from pathlib import Path

import yaml

# Raíz del proyecto (ruta absoluta), para que las rutas de salida
# funcionen sin importar desde dónde se ejecute el proceso.
RAIZ = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    # Captura
    fuente: str = "pantalla"        # "pantalla" | "camara"
    camara_fuente: str = "0"        # índice USB o URL RTSP (si fuente="camara")
    nombre_camara: str = "camara"   # nombre legible para identificar en el log
    region: list | None = None   # [left, top, width, height] o None
    monitor: int = 1
    intervalo_segundos: float = 1.0  # ← cada cuánto pedir imagen a la cámara
    aplicar_preset_al_iniciar: bool = True  # detecta la resolución del
        # stream y aplica el preset correspondiente de PRESETS_CAMARA
    rotacion: int = 0           # rotación de la imagen: 0 | 90 | 180 | 270
                                # (en el sentido horario)
    reconectar_segundos: float = 180.0  # reabrir el stream cada N segundos
        # para cortar el buffer acumulado (RTSP/otros). 0 = nunca.

    # Detección
    metodo: str = "ssim"            # ssim | diff | mse
    umbral: float = 0.5            # 0-1: sensibilidad a nivel píxel
                                  # (0.5 ≈ corte clásico de ssim; más
                                  # bajo = más sensible)
    min_area_px: int = 100          # píxeles mínimos de cambio (genérico)
    blur_ksize: int = 5             # desenfoque (elimina ruido de compresión)
    marcar_cambios: bool = False    # False = NO dibujar líneas rojas
    frames_estables: int = 2        # capturas consecutivas para confirmar
                                    # un cambio (filtro anti-parpadeo)
    min_intervalo_eventos: float = 5.0  # segundos mínimos entre eventos

    # Compensación de vibración (registro de imágenes)
    alinear_imagenes: bool = False  # True = compensa vibración de la cámara
    max_desplazamiento: float = 10.0  # máx. desplazamiento a corregir (px)

    # Panel web (configuración del área de análisis)
    web_enabled: bool = True
    web_host: str = "0.0.0.0"
    web_port: int = 5000

    # Registro de eventos
    log_eventos: str = "eventos.jsonl"
    save_changes: bool = True
    output_dir: str = "capturas_cambio"
    max_imagenes: int = 20        # máx. archivos en output_dir (0 = sin límite)
    nivel_log: str = "INFO"

    # IA (análisis por visión de DeepSeek)
    ia_enabled: bool = False     # enviar cada evento a DeepSeek Vision
    ia_model: str = "deepseek-v4-flash-vision-exp"
    ia_api_key: str = ""        # opcional; si vacío usa env DEEPSEEK_API_KEY o ia.key
    ia_detail: str = "low"      # low (512px, más barato) | high | auto

    # Ruta del YAML que originó esta config (para guardar cambios en vivo)
    _ruta_yaml: str = "config.yaml"

    @classmethod
    def from_yaml(cls, path: str = "config.yaml") -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        cfg = cls()
        cfg._ruta_yaml = str(Path(path).resolve())

        c = raw.get("captura", {})
        cfg.fuente = c.get("fuente", cfg.fuente)
        cfg.camara_fuente = str(c.get("camara_fuente", cfg.camara_fuente))
        cfg.nombre_camara = c.get("nombre_camara", cfg.nombre_camara)
        cfg.region = c.get("region", cfg.region)
        cfg.monitor = c.get("monitor", cfg.monitor)
        cfg.intervalo_segundos = c.get("intervalo_segundos", cfg.intervalo_segundos)
        cfg.aplicar_preset_al_iniciar = c.get(
            "aplicar_preset_al_iniciar", cfg.aplicar_preset_al_iniciar)
        cfg.rotacion = int(c.get("rotacion", cfg.rotacion)) % 360
        cfg.reconectar_segundos = float(
            c.get("reconectar_segundos", cfg.reconectar_segundos))

        d = raw.get("deteccion", {})
        cfg.metodo = d.get("metodo", cfg.metodo)
        cfg.umbral = d.get("umbral", cfg.umbral)
        cfg.min_area_px = d.get("min_area_px", cfg.min_area_px)
        cfg.blur_ksize = d.get("blur_ksize", cfg.blur_ksize)
        cfg.marcar_cambios = d.get("marcar_cambios", cfg.marcar_cambios)
        cfg.frames_estables = d.get("frames_estables", cfg.frames_estables)
        cfg.min_intervalo_eventos = d.get("min_intervalo_eventos",
                                          cfg.min_intervalo_eventos)
        cfg.alinear_imagenes = d.get("alinear_imagenes", cfg.alinear_imagenes)
        cfg.max_desplazamiento = d.get("max_desplazamiento",
                                       cfg.max_desplazamiento)

        r = raw.get("registro", {})
        cfg.log_eventos = r.get("log_eventos", cfg.log_eventos)
        cfg.save_changes = r.get("save_changes", cfg.save_changes)
        cfg.output_dir = r.get("output_dir", cfg.output_dir)
        cfg.max_imagenes = r.get("max_imagenes", cfg.max_imagenes)
        cfg.nivel_log = r.get("nivel", cfg.nivel_log)

        # Rutas absolutas respecto a la raíz del proyecto
        cfg.log_eventos = str(RAIZ / cfg.log_eventos)
        cfg.output_dir = str(RAIZ / cfg.output_dir)

        ia = raw.get("ia", {})
        cfg.ia_enabled = ia.get("enabled", cfg.ia_enabled)
        cfg.ia_model = ia.get("model", cfg.ia_model)
        cfg.ia_api_key = ia.get("api_key", cfg.ia_api_key)
        cfg.ia_detail = ia.get("detail", cfg.ia_detail)

        web = raw.get("web", {})
        cfg.web_enabled = web.get("enabled", cfg.web_enabled)
        cfg.web_host = web.get("host", cfg.web_host)
        cfg.web_port = web.get("port", cfg.web_port)

        return cfg

    def a_dict(self) -> dict:
        """Serializa la config al mismo formato de secciones del YAML."""
        def rel(ruta: str) -> str:
            """Ruta relativa a la raíz (para que el YAML siga siendo portable)."""
            try:
                return str(Path(ruta).relative_to(RAIZ))
            except ValueError:
                return ruta

        return {
            "captura": {
                "fuente": self.fuente,
                "camara_fuente": self.camara_fuente,
                "nombre_camara": self.nombre_camara,
                "region": self.region,
                "monitor": self.monitor,
                "intervalo_segundos": self.intervalo_segundos,
                "aplicar_preset_al_iniciar": self.aplicar_preset_al_iniciar,
                "rotacion": int(self.rotacion),
                "reconectar_segundos": self.reconectar_segundos,
            },
            "deteccion": {
                "metodo": self.metodo,
                "umbral": self.umbral,
                "min_area_px": self.min_area_px,
                "blur_ksize": self.blur_ksize,
                "marcar_cambios": self.marcar_cambios,
                "frames_estables": self.frames_estables,
                "min_intervalo_eventos": self.min_intervalo_eventos,
                "alinear_imagenes": self.alinear_imagenes,
                "max_desplazamiento": self.max_desplazamiento,
            },
            "web": {
                "enabled": self.web_enabled,
                "host": self.web_host,
                "port": self.web_port,
            },
            "registro": {
                "log_eventos": rel(self.log_eventos),
                "save_changes": self.save_changes,
                "output_dir": rel(self.output_dir),
                "max_imagenes": self.max_imagenes,
                "nivel": self.nivel_log,
            },
            "ia": {
                "enabled": self.ia_enabled,
                "model": self.ia_model,
                # NOTA: la API key NO se serializa aquí a propósito, para
                # que nunca se escriba a config.yaml (que sí va a git).
                # Se lee de la variable de entorno DEEPSEEK_API_KEY o del
                # archivo ia.key (ignorado por git).
                "detail": self.ia_detail,
            },
        }

    def guardar(self, path: str | None = None):
        """Escribe la configuración actual al YAML (persiste los cambios
        hechos desde el panel web).

        El archivo se regenera por completo: se pierden los comentarios
        escritos a mano, pero el formato de secciones se conserva.
        """
        destino = path or self._ruta_yaml
        with open(destino, "w", encoding="utf-8") as f:
            f.write("# Configuración del Backend de Monitoreo de Paneles\n")
            f.write("# Generado por el panel web al guardar parámetros en vivo.\n")
            yaml.safe_dump(self.a_dict(), f, allow_unicode=True,
                           sort_keys=False)
