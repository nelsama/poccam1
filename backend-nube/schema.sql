-- Esquema de la base de datos (Supabase / Postgres)
-- Sistema de detección de cambios en paneles industriales.
--
-- Ejecutar en Supabase → SQL Editor.
--
-- IMPORTANTE: `analisis.datos` es JSONB para no atar la base al esquema
-- que devuelva el prompt de la IA. Ese JSON puede cambiar sin migración.

-- ── Workers (cámaras registradas) ──────────────────────────────────
-- Cada worker se identifica con su `worker_id` (configurado localmente).
CREATE TABLE IF NOT EXISTS workers (
    worker_id       TEXT PRIMARY KEY,
    nombre          TEXT,                       -- etiqueta para mostrar
    esquema         TEXT,                       -- formato del JSON que produce
    concentrador_id TEXT,                       -- a qué concentrador pertenece
    ultima_vista    TIMESTAMPTZ,                -- último heartbeat recibido
    creado          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Configuración por worker ───────────────────────────────────────
-- Lo que la nube le manda al concentrador para cada cámara.
-- `version` se incrementa en cada cambio (para el GET condicional).
CREATE TABLE IF NOT EXISTS config_workers (
    worker_id   TEXT PRIMARY KEY REFERENCES workers(worker_id)
                ON DELETE CASCADE,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,  -- bloques de config
    version     INTEGER NOT NULL DEFAULT 1,          -- versión por worker
    actualizado TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Eventos detectados (cambios) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS eventos (
    id          BIGSERIAL PRIMARY KEY,
    worker_id   TEXT NOT NULL,
    evento_id   TEXT UNIQUE,        -- id local del worker (evita duplicados)
    timestamp   TIMESTAMPTZ NOT NULL,
    score       REAL,
    area_px     INTEGER,
    area_borde  INTEGER,
    metodo      TEXT,
    recibido    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_eventos_worker
    ON eventos (worker_id, timestamp DESC);

-- ── Análisis de IA ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS analisis (
    id          BIGSERIAL PRIMARY KEY,
    worker_id   TEXT NOT NULL,
    evento_id   TEXT UNIQUE,        -- 1 análisis por evento (evita duplicados)
    timestamp   TIMESTAMPTZ NOT NULL,
    esquema     TEXT,               -- "personas_v1", "display_v1", ...
    datos       JSONB,              -- JSON LIBRE que devolvió la IA
    modelo      TEXT,
    detail      TEXT,
    latencia_ms INTEGER,
    area_px     INTEGER,
    score       REAL,
    recibido    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_analisis_worker
    ON analisis (worker_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_analisis_esquema
    ON analisis (esquema);
-- Índice GIN para consultar DENTRO del JSON libre
CREATE INDEX IF NOT EXISTS idx_analisis_datos
    ON analisis USING GIN (datos);

-- ── Heartbeats del concentrador ────────────────────────────────────
CREATE TABLE IF NOT EXISTS heartbeats (
    id               BIGSERIAL PRIMARY KEY,
    concentrador_id  TEXT,
    timestamp        TIMESTAMPTZ NOT NULL,
    workers          JSONB,          -- estado de cada worker
    recibido         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Consultas de ejemplo (referencia) ──────────────────────────────
-- Últimos análisis de una cámara:
--   SELECT * FROM analisis WHERE worker_id = 'panel-secado-1'
--   ORDER BY timestamp DESC LIMIT 50;
--
-- Filtrar dentro del JSON libre:
--   SELECT timestamp, datos->>'valor_display' FROM analisis
--   WHERE esquema = 'display_v1' AND datos->>'estado' = 'calentando';
