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

    # Detección
    metodo: str = "ssim"            # ssim | diff | mse
    umbral: float = 0.02            # 0-1, más bajo = más sensible (dígitos)
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

    # IA (futuro, por ahora sin uso)
    ia_enabled: bool = False

    @classmethod
    def from_yaml(cls, path: str = "config.yaml") -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        cfg = cls()

        c = raw.get("captura", {})
        cfg.fuente = c.get("fuente", cfg.fuente)
        cfg.camara_fuente = str(c.get("camara_fuente", cfg.camara_fuente))
        cfg.nombre_camara = c.get("nombre_camara", cfg.nombre_camara)
        cfg.region = c.get("region", cfg.region)
        cfg.monitor = c.get("monitor", cfg.monitor)
        cfg.intervalo_segundos = c.get("intervalo_segundos", cfg.intervalo_segundos)

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

        web = raw.get("web", {})
        cfg.web_enabled = web.get("enabled", cfg.web_enabled)
        cfg.web_host = web.get("host", cfg.web_host)
        cfg.web_port = web.get("port", cfg.web_port)

        return cfg
