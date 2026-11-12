import { useState, useEffect } from 'react'
import { getBranding } from '../api'

// Lee /api/branding al iniciar. Devuelve el modo de branding que el
// backend reporta, o null si el endpoint no existe (api_gaceta.py / 1.py).
// Cuando devuelve "fcpn" el frontend usa el logo del escudo FCPN y el
// borde giratorio bicolor mientras la IA genera la respuesta.
export function useBranding() {
  const [mode, setMode] = useState(null)

  useEffect(() => {
    let activo = true
    getBranding()
      .then((b) => {
        if (activo) setMode(b?.mode || null)
      })
      .catch(() => {
        if (activo) setMode(null)
      })
    return () => {
      activo = false
    }
  }, [])

  return mode
}
