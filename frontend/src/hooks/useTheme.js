import { useState, useEffect, useCallback } from 'react'

const STORAGE_KEY = 'gaceta-theme'

// Maneja el modo claro / oscuro. El tema inicial ya fue aplicado por el
// script de index.html (para evitar parpadeo); aqui solo lo sincronizamos.
export function useTheme() {
  const [theme, setTheme] = useState(() =>
    document.documentElement.classList.contains('dark') ? 'dark' : 'light',
  )

  useEffect(() => {
    const root = document.documentElement
    if (theme === 'dark') root.classList.add('dark')
    else root.classList.remove('dark')
    try {
      localStorage.setItem(STORAGE_KEY, theme)
    } catch {
      // localStorage no disponible: el tema sigue funcionando en memoria.
    }
  }, [theme])

  const toggleTheme = useCallback(() => {
    setTheme((t) => (t === 'dark' ? 'light' : 'dark'))
  }, [])

  return { theme, toggleTheme }
}
