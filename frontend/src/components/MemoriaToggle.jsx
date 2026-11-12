// Suiche pequeno para activar/desactivar la "memoria" de la conversacion
// (que el motor recuerde los turnos anteriores como contexto). Espejo de
// ListasToggle: se renderiza junto al boton de tema. Solo aparece si el
// backend lo soporta (GET /api/memoria).
export default function MemoriaToggle({ habilitada, cargando, onToggle }) {
  const titulo = habilitada
    ? 'Memoria de la conversación: ACTIVADA — clic para desactivar'
    : 'Memoria de la conversación: DESACTIVADA — clic para activar'

  return (
    <div className="flex items-center gap-2 select-none">
      <span className="text-[10px] uppercase tracking-wider font-semibold opacity-70">
        Memoria
      </span>
      <button
        onClick={onToggle}
        disabled={cargando}
        role="switch"
        aria-checked={habilitada}
        aria-label="Activar o desactivar la memoria de la conversación"
        title={titulo}
        className={`relative w-11 h-6 rounded-full border-2 transition-colors ${
          habilitada
            ? 'bg-black dark:bg-white border-black dark:border-white'
            : 'bg-transparent border-black dark:border-white'
        } ${cargando ? 'opacity-50 cursor-wait' : 'cursor-pointer'}`}
      >
        <span
          className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full transition-transform duration-200 ${
            habilitada
              ? 'translate-x-5 bg-white dark:bg-black'
              : 'translate-x-0 bg-black dark:bg-white'
          }`}
        />
      </button>
    </div>
  )
}
