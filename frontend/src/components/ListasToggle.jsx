// Suiche pequeno para activar/desactivar la "consulta a las listas".
// Se renderiza junto al boton de tema (ThemeToggle) en la esquina
// superior derecha. Solo aparece si el backend lo soporta (api_gaceta1.py).
export default function ListasToggle({ habilitadas, cargando, onToggle }) {
  const titulo = habilitadas
    ? 'Consulta a listas: ACTIVADA — clic para desactivar'
    : 'Consulta a listas: DESACTIVADA — clic para activar'

  return (
    <div className="flex items-center gap-2 select-none">
      <span className="text-[10px] uppercase tracking-wider font-semibold opacity-70">
        Listas
      </span>
      <button
        onClick={onToggle}
        disabled={cargando}
        role="switch"
        aria-checked={habilitadas}
        aria-label="Activar o desactivar consulta a listas"
        title={titulo}
        className={`relative w-11 h-6 rounded-full border-2 transition-colors ${
          habilitadas
            ? 'bg-black dark:bg-white border-black dark:border-white'
            : 'bg-transparent border-black dark:border-white'
        } ${cargando ? 'opacity-50 cursor-wait' : 'cursor-pointer'}`}
      >
        <span
          className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full transition-transform duration-200 ${
            habilitadas
              ? 'translate-x-5 bg-white dark:bg-black'
              : 'translate-x-0 bg-black dark:bg-white'
          }`}
        />
      </button>
    </div>
  )
}
