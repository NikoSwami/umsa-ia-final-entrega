// Boton circular que alterna entre modo claro y oscuro.
export default function ThemeToggle({ theme, onToggle }) {
  const esOscuro = theme === 'dark'

  return (
    <button
      onClick={onToggle}
      title={esOscuro ? 'Cambiar a modo claro' : 'Cambiar a modo oscuro'}
      aria-label="Cambiar tema"
      className="w-10 h-10 rounded-full border border-black dark:border-white flex items-center justify-center text-black dark:text-white hover:bg-black/10 dark:hover:bg-white/10 transition-colors duration-200"
    >
      <span className="material-symbols-outlined text-lg">
        {esOscuro ? 'light_mode' : 'dark_mode'}
      </span>
    </button>
  )
}
