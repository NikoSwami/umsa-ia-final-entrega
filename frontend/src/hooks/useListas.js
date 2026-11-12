import { useState, useEffect, useCallback } from 'react'
import { getListas, setListas } from '../api'

// Hook para el interruptor de "consulta a las listas".
// - Al montar pregunta GET /api/listas:
//     - Si responde OK -> el backend lo soporta (api_gaceta1.py).
//     - Si falla (404 con api_gaceta.py o sin red) -> soportado=false y
//       la UI debe ocultar el interruptor.
// - toggle() invierte el estado optimistamente y lo persiste con POST.
export function useListas() {
  const [habilitadas, setHabilitadas] = useState(true)
  const [soportado, setSoportado] = useState(false)
  const [cargando, setCargando] = useState(false)

  useEffect(() => {
    let activo = true
    getListas()
      .then((s) => {
        if (!activo) return
        setHabilitadas(!!s.habilitadas)
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
    const nuevo = !habilitadas
    setCargando(true)
    setHabilitadas(nuevo) // optimista
    try {
      const s = await setListas(nuevo)
      setHabilitadas(!!s.habilitadas)
    } catch {
      setHabilitadas(!nuevo) // revertir si fallo
    } finally {
      setCargando(false)
    }
  }, [habilitadas, cargando])

  return { soportado, habilitadas, cargando, toggle }
}
