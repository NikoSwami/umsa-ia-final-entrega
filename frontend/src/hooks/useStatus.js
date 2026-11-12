import { useState, useEffect } from 'react'
import { getStatus } from '../api'

// Consulta periodicamente el estado del motor de IA hasta que termina
// de cargar (o falla). Devuelve { loading, loaded, error }.
export function useStatus() {
  const [estado, setEstado] = useState({
    loading: true,
    loaded: false,
    error: '',
  })

  useEffect(() => {
    let activo = true
    let timer = null

    const consultar = async () => {
      try {
        const s = await getStatus()
        if (!activo) return
        setEstado(s)
        // Seguir consultando solo mientras el motor no este listo ni en error.
        if (!s.loaded && !s.error) {
          timer = setTimeout(consultar, 2000)
        }
      } catch {
        if (!activo) return
        // El servidor aun no responde: reintentar.
        timer = setTimeout(consultar, 3000)
      }
    }

    consultar()

    return () => {
      activo = false
      if (timer) clearTimeout(timer)
    }
  }, [])

  return estado
}
