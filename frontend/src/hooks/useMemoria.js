import { useState, useEffect, useCallback } from 'react'
import { getMemoria, setMemoria } from '../api'

// Hook para el interruptor de "memoria" (contexto de la conversacion).
// - Al montar pregunta GET /api/memoria:
//     - Si responde OK -> el backend lo soporta.
//     - Si falla (404 / sin red) -> soportado=false y la UI oculta el suiche.
// - toggle() invierte el estado optimistamente y lo persiste con POST.
export function useMemoria() {
  const [habilitada, setHabilitada] = useState(true)
  const [soportado, setSoportado] = useState(false)
  const [cargando, setCargando] = useState(false)

  useEffect(() => {
    let activo = true
    getMemoria()
      .then((s) => {
        if (!activo) return
        setHabilitada(!!s.habilitada)
        setSoportado(true)
      })
      .catch(() => {
        if (activo) setSoportado(false)
      })
    return () => {
      activo = false
    }
  }, [])

  const toggle = useCallback(async () => {
    if (cargando) return
    const nuevo = !habilitada
    setCargando(true)
    setHabilitada(nuevo) // optimista
    try {
      const s = await setMemoria(nuevo)
      setHabilitada(!!s.habilitada)
    } catch {
      setHabilitada(!nuevo) // revertir si fallo
    } finally {
      setCargando(false)
    }
  }, [habilitada, cargando])

  return { soportado, habilitada, cargando, toggle }
}
