// Indicador del estado del motor de IA (cargando / listo / error).
export default function StatusBadge({ estado }) {
  let color, texto, icono, animar

  if (estado.error) {
    color = 'text-red-600 dark:text-red-400'
    texto = 'Error al cargar el modelo'
    icono = 'error'
    animar = false
  } else if (estado.loaded) {
    color = 'text-green-700 dark:text-green-400'
    texto = 'Modelo de IA listo'
    icono = 'check_circle'
    animar = false
  } else {
    color = 'text-black/60 dark:text-white/60'
    texto = 'Cargando modelo de IA...'
    icono = 'progress_activity'
    animar = true
  }

  return (
    <div
      className={`flex items-center gap-2 text-xs font-semibold ${color}`}
      title={estado.error || texto}
    >
      <span
        className={`material-symbols-outlined text-base ${
          animar ? 'animate-spin' : ''
        }`}
      >
        {icono}
      </span>
      <span className="truncate">{texto}</span>
    </div>
  )
}
