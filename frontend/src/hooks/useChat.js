import { useState, useEffect, useCallback, useRef } from 'react'
import { streamChat, nuevaConversacion as apiNueva } from '../api'

const STORAGE_KEY = 'gaceta-conversaciones'

function nuevoId() {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) {
    return crypto.randomUUID()
  }
  return `c-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function crearConversacion() {
  return { id: nuevoId(), title: 'Nueva conversación', messages: [] }
}

function cargarConversaciones() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    const arr = raw ? JSON.parse(raw) : null
    if (Array.isArray(arr) && arr.length > 0) return arr
  } catch {
    // JSON corrupto o sin acceso: empezamos limpio.
  }
  return [crearConversacion()]
}

// Maneja la lista de conversaciones (historial), la conversacion activa
// y el envio de mensajes con respuesta en streaming.
export function useChat() {
  const [conversaciones, setConversaciones] = useState(cargarConversaciones)
  const [activaId, setActivaId] = useState(() => conversaciones[0].id)
  const [enviando, setEnviando] = useState(false)
  const abortRef = useRef(null)

  // Persistir el historial en localStorage.
  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(conversaciones))
    } catch {
      // Sin localStorage: el historial vive solo en memoria.
    }
  }, [conversaciones])

  const activa =
    conversaciones.find((c) => c.id === activaId) ?? conversaciones[0]

  // Aplica una transformacion solo a la conversacion activa.
  const actualizarActiva = useCallback(
    (fn) => {
      setConversaciones((prev) =>
        prev.map((c) => (c.id === activaId ? fn(c) : c)),
      )
    },
    [activaId],
  )

  const nuevaConversacion = useCallback(async () => {
    // Cortar cualquier respuesta en curso: sin esto, el stream seguiria
    // escribiendo en la conversacion vieja y `enviando` quedaba en true,
    // dejando la caja de texto bloqueada para siempre.
    abortRef.current?.abort()
    const nueva = crearConversacion()
    setConversaciones((prev) => {
      // Descartar la conversacion activa si quedo vacia (sin mensajes).
      const limpio = prev.filter(
        (c) => c.id !== activaId || c.messages.length > 0,
      )
      return [nueva, ...limpio]
    })
    setActivaId(nueva.id)
    try {
      await apiNueva()
    } catch {
      // Si el backend no responde, la conversacion local igual se crea.
    }
  }, [activaId])

  const seleccionarConversacion = useCallback(
    async (id) => {
      if (id === activaId) return
      // Cortar la respuesta en curso antes de cambiar de vista (ver nota en
      // nuevaConversacion): evita que el stream escriba en la conversacion
      // equivocada y que la caja quede bloqueada.
      abortRef.current?.abort()
      setActivaId(id)
      // El motor mantiene un solo historial conversacional: al cambiar de
      // conversacion lo reiniciamos para no mezclar contextos.
      try {
        await apiNueva()
      } catch {
        // Sin conexion: solo cambia la vista.
      }
    },
    [activaId],
  )

  const enviarMensaje = useCallback(
    async (texto) => {
      const mensaje = (texto || '').trim()
      if (!mensaje || enviando) return

      setEnviando(true)
      const controller = new AbortController()
      abortRef.current = controller

      // Anadir el mensaje del usuario + un hueco para la respuesta.
      actualizarActiva((c) => ({
        ...c,
        title: c.messages.length === 0 ? mensaje.slice(0, 40) : c.title,
        messages: [
          ...c.messages,
          { id: nuevoId(), role: 'user', content: mensaje },
          { id: nuevoId(), role: 'assistant', content: '' },
        ],
      }))

      // Reemplaza el contenido del ultimo mensaje (la respuesta del asistente).
      const setRespuesta = (contenido) => {
        actualizarActiva((c) => {
          const msgs = c.messages.slice()
          if (msgs.length > 0) {
            msgs[msgs.length - 1] = {
              ...msgs[msgs.length - 1],
              content: contenido,
            }
          }
          return { ...c, messages: msgs }
        })
      }

      try {
        for await (const evt of streamChat(mensaje, controller.signal)) {
          if (evt.type === 'chunk') {
            setRespuesta(evt.content)
          } else if (evt.type === 'error') {
            setRespuesta(`⚠️ ${evt.content || 'Error desconocido.'}`)
          }
        }
      } catch (e) {
        if (e.name !== 'AbortError') {
          setRespuesta(
            '⚠️ No se pudo conectar con el servidor. ' +
              'Verifica que el backend esté en ejecución.',
          )
        }
      } finally {
        setEnviando(false)
        abortRef.current = null
      }
    },
    [enviando, actualizarActiva],
  )

  return {
    conversaciones,
    activa,
    activaId,
    enviando,
    enviarMensaje,
    nuevaConversacion,
    seleccionarConversacion,
  }
}
