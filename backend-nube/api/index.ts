/**
 * API del sistema externo (Vercel Serverless Function).
 *
 * Implementa el lado servidor del contrato `CONTRATO_NUBE.md`:
 *
 *   GET  /api/concentrador/config?version=N  → config vigente para los workers
 *   POST /api/concentrador/analisis          → recibe un análisis de la IA
 *   POST /api/concentrador/estado            → heartbeat del concentrador
 *   POST /api/concentrador/config            → recibe config publicada local
 *
 *   GET  /api/workers                        → lista de workers conocidos
 *   GET  /api/analisis?worker_id=...&limit=  → consulta de análisis
 *   GET  /api/eventos?worker_id=...          → consulta de eventos
 *
 * Todas las llamadas del concentrador se autentican con Bearer token.
 */

import { supabase, autenticado, json } from '../lib/supabase.js';

// Runtime Node.js (no Edge): las variables de entorno se inyectan de forma
// fiable; en Edge se comportaban de manera inconsistente.
export const config = { runtime: 'nodejs' };

/**
 * Adaptador Vercel Node → interfaz tipo Fetch usada por el resto del código.
 * `@vercel/node` entrega (req, res) con Node streams.
 */
interface ReqNode {
  method?: string;
  url?: string;
  headers: Record<string, string | string[] | undefined>;
  on: (evento: string, cb: (trozo?: unknown) => void) => void;
}

interface ResNode {
  statusCode: number;
  setHeader: (k: string, v: string) => void;
  end: (cuerpo?: string) => void;
}

function leerCuerpo(req: ReqNode): Promise<string> {
  return new Promise((resolve) => {
    let datos = '';
    req.on('data', (t) => {
      datos += String(t);
    });
    req.on('end', () => resolve(datos));
  });
}

export default async function handler(req: ReqNode, res: ResNode): Promise<void> {
  const url = new URL(req.url ?? '/', 'https://local');
  const cuerpo = req.method === 'GET' ? '' : await leerCuerpo(req);

  const peticion = {
    method: (req.method ?? 'GET').toUpperCase(),
    url: url.toString(),
    headers: new Map(
      Object.entries(req.headers).map(([k, v]) => [k, Array.isArray(v) ? v.join(',') : String(v ?? '')]),
    ),
    json: async () => (cuerpo ? JSON.parse(cuerpo) : {}),
  };

  const respuesta = await manejar(peticion, url);
  const texto = await respuesta.text();
  res.statusCode = respuesta.status;
  res.setHeader('Content-Type', respuesta.headers.get('Content-Type') ?? 'application/json');
  res.end(texto);
}
interface Peticion {
  method: string;
  url: string;
  headers: Map<string, string>;
  json: () => Promise<Record<string, unknown>>;
}

async function manejar(peticion: Peticion, url: URL): Promise<Response> {
  const ruta = url.pathname.replace(/\/+$/, '');
  const metodo = peticion.method;

  try {
    // ── Endpoints del concentrador (requieren token) ────────────────
    if (ruta.startsWith('/api/concentrador/')) {
      if (!autenticado(peticion)) {
        return json(401, { error: 'token inválido' });
      }
      return await rutasConcentrador(ruta, metodo, peticion, url);
    }

    // ── Consultas de lectura (para dashboards) ──────────────────────
    if (ruta === '/api/workers' && metodo === 'GET') {
      return await listarWorkers();
    }
    if (ruta === '/api/analisis' && metodo === 'GET') {
      return await listarAnalisis(url);
    }
    if (ruta === '/api/eventos' && metodo === 'GET') {
      return await listarEventos(url);
    }
    if (ruta === '/api/salud' || ruta === '/api/health') {
      return json(200, { ok: true, servicio: 'poccam-backend-nube' });
    }

    return json(404, { error: 'ruta no encontrada', ruta });
  } catch (e) {
    const mensaje = e instanceof Error ? e.message : String(e);
    return json(500, { error: mensaje });
  }
}

// ── Rutas del concentrador ───────────────────────────────────────────

async function rutasConcentrador(
  ruta: string,
  metodo: string,
  req: Peticion,
  url: URL,
): Promise<Response> {
  const db = supabase();

  // GET config?version=N → devuelve la config de todos los workers.
  // Si la versión no cambió, responde 304 (ahorro de ancho de banda).
  if (ruta === '/api/concentrador/config' && metodo === 'GET') {
    const versionPedida = Number(url.searchParams.get('version') ?? '-1');

    const { data, error } = await db
      .from('config_workers')
      .select('worker_id, payload, version');
    if (error) return json(500, { error: error.message });

    const filas = (data ?? []) as Array<{
      worker_id: string;
      payload: Record<string, unknown>;
      version: number;
    }>;
    // Versión global = suma de versiones por worker (cambia si alguno cambia)
    const versionGlobal = filas.reduce(
      (acc: number, f) => acc + Number(f.version),
      0,
    );

    if (versionPedida === versionGlobal) {
      return new Response(null, { status: 304 });
    }

    const workers: Record<string, unknown> = {};
    for (const f of filas) workers[f.worker_id] = f.payload;

    return json(200, { version: versionGlobal, workers });
  }

  // POST config → el concentrador publica cambios hechos en el front local
  if (ruta === '/api/concentrador/config' && metodo === 'POST') {
    const cuerpo = (await req.json()) as { workers?: Record<string, unknown> };
    const workers = cuerpo.workers ?? {};
    for (const [workerId, payload] of Object.entries(workers)) {
      await upsertConfig(db, workerId, payload as Record<string, unknown>);
    }
    return json(200, { ok: true, actualizados: Object.keys(workers).length });
  }

  // POST analisis → recibe un análisis de la IA
  if (ruta === '/api/concentrador/analisis' && metodo === 'POST') {
    const sobre = (await req.json()) as Record<string, unknown>;
    const workerId = sobre.worker_id as string | undefined;
    if (!workerId) return json(400, { error: 'falta worker_id' });

    const meta = (sobre.meta ?? {}) as Record<string, unknown>;

    // Registrar/actualizar el worker (auto-descubrimiento)
    await upsertWorker(db, workerId, sobre.esquema as string | undefined);

    const { error } = await db.from('analisis').upsert(
      {
        worker_id: workerId,
        evento_id: (sobre.evento_id as string) ?? null,
        timestamp: (sobre.timestamp as string) ?? new Date().toISOString(),
        esquema: (sobre.esquema as string) ?? null,
        datos: sobre.datos ?? {},
        modelo: (meta.modelo as string) ?? null,
        detail: (meta.detail as string) ?? null,
        latencia_ms: (meta.latencia_ms as number) ?? null,
        area_px: (meta.area_px as number) ?? null,
        score: (meta.score as number) ?? null,
      },
      { onConflict: 'evento_id', ignoreDuplicates: true },
    );
    if (error) return json(500, { error: error.message });

    return json(200, { ok: true, recibido: sobre.evento_id });
  }

  // POST estado → heartbeat con el estado de cada worker
  if (ruta === '/api/concentrador/estado' && metodo === 'POST') {
    const cuerpo = (await req.json()) as Record<string, unknown>;
    const { error } = await db.from('heartbeats').insert({
      concentrador_id: (cuerpo.concentrador_id as string) ?? null,
      timestamp: (cuerpo.timestamp as string) ?? new Date().toISOString(),
      workers: cuerpo.workers ?? {},
    });
    if (error) return json(500, { error: error.message });

    // Actualizar "ultima_vista" de cada worker reportado
    for (const workerId of Object.keys(
      (cuerpo.workers ?? {}) as Record<string, unknown>,
    )) {
      await db
        .from('workers')
        .upsert(
          { worker_id: workerId, ultima_vista: new Date().toISOString() },
          { onConflict: 'worker_id' },
        );
    }
    return json(200, { ok: true });
  }

  return json(404, { error: 'ruta de concentrador no encontrada', ruta });
}

// ── Helpers de base de datos ─────────────────────────────────────────

/* eslint-disable @typescript-eslint/no-explicit-any */
async function upsertWorker(db: any, workerId: string, esquema?: string) {
  await db.from('workers').upsert(
    {
      worker_id: workerId,
      esquema: esquema ?? null,
      ultima_vista: new Date().toISOString(),
    },
    { onConflict: 'worker_id' },
  );
}

async function upsertConfig(
  db: any,
  workerId: string,
  payload: Record<string, unknown>,
) {
  const { data } = await db
    .from('config_workers')
    .select('version')
    .eq('worker_id', workerId)
    .maybeSingle();
  const version = Number(data?.version ?? 0) + 1;

  await db.from('config_workers').upsert(
    {
      worker_id: workerId,
      payload,
      version,
      actualizado: new Date().toISOString(),
    },
    { onConflict: 'worker_id' },
  );
}

// ── Consultas de lectura ─────────────────────────────────────────────

async function listarWorkers(): Promise<Response> {
  const db = supabase();
  const { data, error } = await db
    .from('workers')
    .select('worker_id, nombre, esquema, ultima_vista')
    .order('worker_id');
  if (error) return json(500, { error: error.message });
  return json(200, { workers: data ?? [] });
}

async function listarAnalisis(url: URL): Promise<Response> {
  const db = supabase();
  const workerId = url.searchParams.get('worker_id');
  const limite = Math.min(Number(url.searchParams.get('limit') ?? '50'), 500);

  let consulta = db
    .from('analisis')
    .select('*')
    .order('timestamp', { ascending: false })
    .limit(limite);
  if (workerId) consulta = consulta.eq('worker_id', workerId);

  const { data, error } = await consulta;
  if (error) return json(500, { error: error.message });
  return json(200, { analisis: data ?? [] });
}

async function listarEventos(url: URL): Promise<Response> {
  const db = supabase();
  const workerId = url.searchParams.get('worker_id');
  const limite = Math.min(Number(url.searchParams.get('limit') ?? '50'), 500);

  let consulta = db
    .from('eventos')
    .select('*')
    .order('timestamp', { ascending: false })
    .limit(limite);
  if (workerId) consulta = consulta.eq('worker_id', workerId);

  const { data, error } = await consulta;
  if (error) return json(500, { error: error.message });
  return json(200, { eventos: data ?? [] });
}
