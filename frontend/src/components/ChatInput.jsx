import { useState, useRef, useEffect } from 'react'

// Caja de texto al pie de la pantalla, siempre visible (ocupa su propio
// espacio en el layout, no flota sobre el chat). Crece con el contenido,
// envia con Enter (Shift+Enter para salto de linea).
export default function ChatInput({ onEnviar, enviando, modeloListo }) {
  const [texto, setTexto] = useState('')
  const textareaRef = useRef(null)
  const enviandoAnteriorRef = useRef(enviando)

  // Devolver el foco a la caja de texto SIN usar el mouse: al enviar (clic
  // en el boton pierde el foco) y cuando termina de generarse la respuesta
  // (cubre tambien los mensajes enviados desde un chip de sugerencia, que
  // nunca pasaron por esta caja). No dispara en el montaje inicial, solo
  // en la transicion true -> false.
  useEffect(() => {
    if (enviandoAnteriorRef.current && !enviando) {
      textareaRef.current?.focus()
    }
    enviandoAnteriorRef.current = enviando
  }, [enviando])

  // Ajustar la altura del textarea al contenido (hasta un maximo de 192px).
  // Cuando esta vacio dejamos que mande el CSS (min-height): asi evitamos
  // una medicion erronea en el primer render —antes de que el layout se
  // asiente— que dejaba la caja demasiado alta.
  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    if (texto === '') {
      el.style.height = ''
      return
    }
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 192)}px`
  }, [texto])

  const bloqueado = enviando || !modeloListo

  const enviar = () => {
    const limpio = texto.trim()
    if (!limpio || bloqueado) return
    onEnviar(limpio)
    setTexto('')
    textareaRef.current?.focus()
  }

  const onKeyDown = (e) => {
    // Enter envia SOLO si ya se puede enviar. Mientras se genera la respuesta
    // (bloqueado) NO enviamos, pero tampoco bloqueamos la tecla: se comporta
    // como un Enter normal (salto de linea) para poder ir redactando.
    if (e.key === 'Enter' && !e.shiftKey && !bloqueado) {
      e.preventDefault()
      enviar()
    }
  }

  let placeholder = 'Escribe tu consulta aquí...'
  if (!modeloListo) placeholder = 'El modelo de IA se está cargando…'
  else if (enviando) placeholder = 'Puedes ir escribiendo tu siguiente consulta…'

  return (
    <div className="shrink-0 bg-umsa-blanco dark:bg-black border-t-2 border-black/20 dark:border-white/20 py-6 px-6 md:px-12 flex justify-center">
      <div className="w-full max-w-3xl">
        <div className="border-2 border-black dark:border-white bg-white dark:bg-black flex items-end p-2 transition-colors focus-within:border-black/70 dark:focus-within:border-white/70">
          <textarea
            ref={textareaRef}
            value={texto}
            onChange={(e) => setTexto(e.target.value)}
            onKeyDown={onKeyDown}
            rows={1}
            placeholder={placeholder}
            className="w-full bg-transparent border-none outline-none focus:outline-none focus:ring-0 resize-none max-h-48 py-3 px-4 text-black dark:text-white placeholder:text-black/40 dark:placeholder:text-white/40 disabled:opacity-60"
            style={{ minHeight: '52px' }}
          />
          <button
            onClick={enviar}
            disabled={bloqueado || !texto.trim()}
            aria-label="Enviar"
            className="p-3 text-black dark:text-white hover:bg-black/10 dark:hover:bg-white/10 transition-colors m-1 border border-transparent hover:border-black/30 dark:hover:border-white/30 shrink-0 flex items-center justify-center disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent"
          >
            <span className="material-symbols-outlined fill">send</span>
          </button>
        </div>
        <div className="text-center mt-3 text-black/40 dark:text-white/40 text-xs">
          La IA puede cometer errores. Verifica la información importante con
          documentos oficiales.
        </div>
      </div>
    </div>
  )
}
