import { useRef, useEffect } from 'react'
import ChatMessage from './ChatMessage'

// Mensaje de bienvenida que se muestra cuando la conversacion esta vacia.
// El avatar siempre es el icono "smart_toy" (no cambia con branding).
// Unico boton: "¿Cuáles son tus funciones?" -> lo atiende el PASO 0a de
// motor_web._pipeline_stream con RESPUESTA_CAPACIDADES (texto fijo, sin LLM).
function Bienvenida({ onSugerencia, modeloListo }) {
  return (
    <div className="flex gap-4">
      <div className="w-10 h-10 rounded-full border-2 border-black dark:border-white flex items-center justify-center shrink-0 bg-black/5 dark:bg-white/5 text-black dark:text-white">
        <span className="material-symbols-outlined text-xl">smart_toy</span>
      </div>
      <div className="flex flex-col gap-2 max-w-[85%]">
        <div className="text-black/50 dark:text-white/50 text-xs font-semibold uppercase tracking-wider">
          Gaceta IA
        </div>
        <div className="border-2 border-black dark:border-white p-5 leading-relaxed bg-white dark:bg-black text-black dark:text-white">
          <p>
            Hola. Soy el asistente virtual de la Gaceta Universitaria de la
            UMSA. ¿En qué te puedo ayudar hoy?
          </p>
          <p className="mt-4 text-black/70 dark:text-white/70 text-sm">
            Puedes preguntarme sobre resoluciones del Honorable Consejo
            Universitario, reglamentos vigentes, estatutos y normativas
            académicas.
          </p>
          <p className="mt-2 text-black/50 dark:text-white/50 text-xs">
            Si quieres más precisión con un nombre o un texto concreto,
            escríbelo entre comillas (dobles "así" o simples 'así'): así lo
            busco de forma exacta.
          </p>
          <div className="mt-5">
            {/* Borde a contraste pleno + inversion al hover: el mismo gesto
                brutalista del recuadro que lo contiene. `enabled:` evita que
                el hover dispare mientras el modelo carga. */}
            <button
              onClick={() => modeloListo && onSugerencia && onSugerencia('¿Cuáles son tus funciones?')}
              disabled={!modeloListo}
              title={modeloListo ? 'Ver las funciones del asistente' : 'El modelo se está cargando…'}
              className="inline-flex items-center gap-2 text-left text-sm font-medium
                         border-2 border-black dark:border-white px-4 py-2
                         text-black dark:text-white
                         transition-colors duration-150
                         enabled:hover:bg-black enabled:hover:text-white
                         dark:enabled:hover:bg-white dark:enabled:hover:text-black
                         focus-visible:outline-none focus-visible:ring-2
                         focus-visible:ring-black dark:focus-visible:ring-white
                         focus-visible:ring-offset-2 focus-visible:ring-offset-white
                         dark:focus-visible:ring-offset-black
                         disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <span className="material-symbols-outlined text-base">info</span>
              ¿Cuáles son tus funciones?
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

// Area de chat con su propio desplazamiento. Mantiene la vista pegada al
// final cuando llegan mensajes nuevos o cambia el contenido en streaming.
// `branding` se pasa a cada ChatMessage para que el ultimo mensaje del
// asistente (mientras se genera) pueda usar el borde giratorio bicolor.
export default function ChatArea({ mensajes, enviando, branding, onSugerencia, modeloListo }) {
  const scrollRef = useRef(null)

  // Auto-scroll "inteligente":
  //   - pegadoAbajoRef = true  -> cada update lleva la vista al fondo
  //   - pegadoAbajoRef = false -> el usuario subio a leer, lo respetamos
  // La bandera se actualiza con el evento onScroll mas abajo; aqui solo la
  // re-enganchamos cuando llega un mensaje NUEVO (cambia el numero total)
  // o se cambia de conversacion. Asi durante el streaming de tokens del
  // mismo mensaje, si subiste, te quedas arriba sin que la vista brinque.
  const pegadoAbajoRef = useRef(true)
  const ultimoConteoRef = useRef(0)

  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    if (mensajes.length !== ultimoConteoRef.current) {
      pegadoAbajoRef.current = true
    }
    ultimoConteoRef.current = mensajes.length
    if (pegadoAbajoRef.current) {
      el.scrollTop = el.scrollHeight
    }
  }, [mensajes, enviando])

  // El usuario desplazo manualmente: dentro de ~60 px del fondo seguimos
  // "pegados", mas arriba no. Este evento se dispara tanto para scroll
  // manual como programatico — los dos casos llevan al estado correcto.
  const onScroll = () => {
    const el = scrollRef.current
    if (!el) return
    const distancia = el.scrollHeight - el.scrollTop - el.clientHeight
    pegadoAbajoRef.current = distancia < 60
  }

  return (
    <div
      ref={scrollRef}
      onScroll={onScroll}
      className="flex-1 overflow-y-auto chat-scroll p-6 md:p-12 pb-8 flex flex-col items-center"
    >
      <div className="w-full max-w-3xl flex flex-col gap-8 mt-12">
        {mensajes.length === 0 ? (
          <Bienvenida onSugerencia={onSugerencia} modeloListo={modeloListo} />
        ) : (
          mensajes.map((m, i) => {
            const esUltimo = i === mensajes.length - 1
            const generando = enviando && esUltimo && m.role === 'assistant'
            const cargando = generando && m.content === ''
            return (
              <ChatMessage
                key={m.id || i}
                role={m.role}
                content={m.content}
                cargando={cargando}
                generando={generando}
                branding={branding}
              />
            )
          })
        )}
      </div>
    </div>
  )
}
