// Cliente HTTP de la API de la Gaceta IA.
// En desarrollo, Vite redirige /api -> http://127.0.0.1:8000 (ver vite.config.js).
// En produccion, FastAPI sirve la API en el mismo origen.

const BASE = '/api'

// Estado de carga del motor de IA: { loading, loaded, error }
export async function getStatus() {
  const res = await fetch(`${BASE}/status`)
  if (!res.ok) throw new Error('No se pudo consultar el estado del servidor')
  return res.json()
}

// Limpia el historial conversacional del motor.
export async function nuevaConversacion() {
  const res = await fetch(`${BASE}/nueva`, { method: 'POST' })
  if (!res.ok) throw new Error('No se pudo iniciar una nueva conversación')
  return res.json()
}

// Estado del interruptor de "consulta a las listas". La API unificada
// (api_gaceta.py) lo soporta. Si por algun motivo respondiera 404 (una
// variante antigua), el hook lo trata como "no soportado" y oculta el suiche.
export async function getListas() {
  const res = await fetch(`${BASE}/listas`)
  if (!res.ok) throw new Error('listas no soportado')
  return res.json() // { habilitadas: true|false }
}

// Activa o desactiva la funcion de consulta a listas en caliente.
export async function setListas(habilitadas) {
  const res = await fetch(`${BASE}/listas`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ habilitadas: !!habilitadas }),
  })
  if (!res.ok) throw new Error('No se pudo cambiar el estado de listas')
  return res.json()
}

// Sugerencias de preguntas construidas desde el CORPUS real (rotan en cada
// llamada). Si el endpoint no existe (variante antigua) -> sin chips.
export async function getSugerencias(n = 4) {
  const res = await fetch(`${BASE}/sugerencias?n=${n}`)
  if (!res.ok) throw new Error('sugerencias no soportadas')
  return res.json() // { sugerencias: ["¿...?", ...] }
}

// Estado del interruptor de "memoria" (que el motor recuerde el contexto de la
// conversacion). Si el endpoint no existe (variante antigua) -> no soportado.
export async function getMemoria() {
  const res = await fetch(`${BASE}/memoria`)
  if (!res.ok) throw new Error('memoria no soportada')
  return res.json() // { habilitada: true|false }
}

// Activa o desactiva la memoria conversacional en caliente.
export async function setMemoria(habilitada) {
  const res = await fetch(`${BASE}/memoria`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ habilitada: !!habilitada }),
  })
  if (!res.ok) throw new Error('No se pudo cambiar el estado de memoria')
  return res.json()
}

// Branding del backend. La API unificada (api_gaceta.py) responde
// {"mode": "fcpn"}. Si el endpoint no existiera (variante antigua) el
// frontend usa el modo por defecto (logo smart_toy, sin borde giratorio).
export async function getBranding() {
  const res = await fetch(`${BASE}/branding`)
  if (!res.ok) throw new Error('branding no soportado')
  return res.json() // { mode: "fcpn", colors: {...} }
}

// Pide al servidor que termine su proceso. Lo usa el boton "Cerrar Sesión".
// keepalive permite que el navegador termine de enviar la peticion aunque
// la pagina se cierre o navegue. Devuelve true si el servidor acepto el
// cierre (o si la conexion se corto porque el servidor ya murio antes de
// responder), y false si el servidor lo rechazo (401: protegido con
// GACETA_ADMIN_TOKEN) para que quien llama pueda avisar sin romper la UI.
export async function shutdown() {
  try {
    const res = await fetch(`${BASE}/shutdown`, { method: 'POST', keepalive: true })
    return res.ok
  } catch {
    return true // ok: el servidor murio antes de responder
  }
}

// Envia un mensaje y devuelve un async iterator de eventos del stream.
// Cada evento es: { type: 'chunk', content } | { type: 'done' } | { type: 'error', content }
export async function* streamChat(message, signal) {
  const res = await fetch(`${BASE}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
    signal,
  })

  if (!res.ok || !res.body) {
    throw new Error('Error de conexión con el servidor')
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })

    // Los eventos SSE estan separados por una linea en blanco.
    const partes = buffer.split('\n\n')
    buffer = partes.pop() ?? ''

    for (const parte of partes) {
      const linea = parte.trim()
      if (linea.startsWith('data:')) {
        const datos = linea.slice(5).trim()
        if (datos) {
          try {
            yield JSON.parse(datos)
          } catch {
            // Fragmento mal formado: lo ignoramos.
          }
        }
      }
    }
  }
}
