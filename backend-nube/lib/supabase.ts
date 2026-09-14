/**
 * Cliente de Supabase y utilidades compartidas.
 *
 * Variables de entorno requeridas (configurar en Vercel → Settings → Env):
 *   SUPABASE_URL          → Project URL (Supabase → Settings → API)
 *   SUPABASE_SERVICE_KEY  → service_role key (¡secreta! solo en el servidor)
 *   CONCENTRADOR_TOKEN    → token que usa el concentrador para autenticarse
 *
 * NOTA: se usa la service_role key porque esta API es servidor-a-servidor
 * (el concentrador), no hay usuarios finales. Salta las políticas RLS.
 */

import { createClient, SupabaseClient } from '@supabase/supabase-js';

// Vercel (Edge/Node) expone `process.env`. Se declara aquí para no
// depender de @types/node en el chequeo de tipos.
declare const process: { env: Record<string, string | undefined> };

let _cliente: SupabaseClient | null = null;

export function supabase(): SupabaseClient {
  if (_cliente) return _cliente;
  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_KEY;
  if (!url || !key) {
    throw new Error(
      'Faltan SUPABASE_URL o SUPABASE_SERVICE_KEY en las variables de entorno',
    );
  }
  _cliente = createClient(url, key, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
  return _cliente;
}

/** Verifica el token del concentrador (Authorization: Bearer <token>). */
export function autenticado(req: { headers: Map<string, string> }): boolean {
  const esperado = process.env.CONCENTRADOR_TOKEN;
  // Si no hay token configurado, se permite (modo desarrollo).
  if (!esperado) return true;
  const cabecera = req.headers.get('authorization') ?? '';
  const token = cabecera.replace(/^Bearer\s+/i, '').trim();
  return token === esperado;
}

/** Respuesta JSON uniforme. */
export function json(codigo: number, cuerpo: unknown) {
  return new Response(JSON.stringify(cuerpo), {
    status: codigo,
    headers: { 'Content-Type': 'application/json' },
  });
}
