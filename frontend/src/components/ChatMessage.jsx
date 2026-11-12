import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

// Un mensaje del chat. Los del usuario van en una caja rellena (invertida);
// los del asistente en una caja con borde y contenido en Markdown.
// Cuando branding === 'fcpn' y el mensaje se esta generando, el borde es
// un degradado giratorio bicolor (azul UMSA + rojo institucional).
// El avatar del asistente siempre es el icono "smart_toy".
export default function ChatMessage({
  role,
  content,
  cargando,    // true cuando el asistente aun no ha emitido tokens
  generando,   // true durante toda la generacion (streaming) del mensaje
  branding,    // "fcpn" para activar el borde giratorio
}) {
  const esUsuario = role === 'user'
  const usarFcpn = branding === 'fcpn'

  if (esUsuario) {
    return (
      <div className="flex gap-4 flex-row-reverse">
        <div className="w-10 h-10 rounded-full border-2 border-black dark:border-white flex items-center justify-center shrink-0 bg-black dark:bg-white text-white dark:text-black">
          <span className="material-symbols-outlined text-xl">person</span>
        </div>
        <div className="flex flex-col gap-2 max-w-[85%] items-end">
          <div className="text-black/50 dark:text-white/50 text-xs font-semibold uppercase tracking-wider">
            Tú
          </div>
          <div className="border-2 border-black dark:border-white bg-black dark:bg-white text-white dark:text-black p-5 leading-relaxed whitespace-pre-wrap">
            {content}
          </div>
        </div>
      </div>
    )
  }

  // Asistente
  const claseBorde =
    generando && usarFcpn
      ? 'fcpn-spinner-border'
      : 'border-2 border-black dark:border-white bg-white dark:bg-black'

  return (
    <div className="flex gap-4">
      <div className="w-10 h-10 rounded-full border-2 border-black dark:border-white flex items-center justify-center shrink-0 bg-black/5 dark:bg-white/5 text-black dark:text-white">
        <span className="material-symbols-outlined text-xl">smart_toy</span>
      </div>

      <div className="flex flex-col gap-2 max-w-[85%]">
        <div className="text-black/50 dark:text-white/50 text-xs font-semibold uppercase tracking-wider">
          Gaceta IA
        </div>
        <div className={`${claseBorde} p-5 leading-relaxed text-black dark:text-white`}>
          {cargando ? (
            <span className="flex items-center gap-2 text-black/60 dark:text-white/60">
              <span className="material-symbols-outlined text-base animate-spin">
                progress_activity
              </span>
              Buscando en la Gaceta…
            </span>
          ) : (
            <div className="prose prose-sm dark:prose-invert max-w-none prose-headings:mt-3 prose-headings:mb-2 prose-p:my-2 prose-pre:bg-black/5 dark:prose-pre:bg-white/10 prose-code:text-system-glow prose-a:text-system-glow">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {content.replace(/^\s+/, '')}
              </ReactMarkdown>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
