import { useState } from 'react'

// Escudo de la Facultad de Ciencias Puras y Naturales (FCPN - UMSA).
// Por defecto intenta cargar /fcpn-logo.png (deja un PNG con la imagen
// real en frontend/public/fcpn-logo.png si quieres el escudo exacto).
// Si la imagen no esta, muestra una aproximacion en SVG inline para que
// el modo FCPN funcione aunque no exista el PNG.
export default function FcpnLogo({ className = '' }) {
  const [usarPng, setUsarPng] = useState(true)

  if (usarPng) {
    return (
      <img
        src="/fcpn-logo.png"
        alt="Facultad de Ciencias Puras y Naturales — UMSA"
        className={`${className} object-contain`}
        onError={() => setUsarPng(false)}
      />
    )
  }

  return <FcpnSvgFallback className={className} />
}

function FcpnSvgFallback({ className = '' }) {
  return (
    <svg
      viewBox="0 0 100 100"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      role="img"
      aria-label="Escudo FCPN UMSA"
    >
      <defs>
        <path id="fcpnTop" d="M 14 50 A 36 36 0 0 1 86 50" fill="none" />
        <path id="fcpnMid" d="M 22 56 A 28 28 0 0 0 78 56" fill="none" />
      </defs>
      {/* Anillo azul exterior */}
      <circle cx="50" cy="50" r="48" fill="#0B2D5C" />
      {/* Disco blanco interior */}
      <circle cx="50" cy="50" r="34" fill="#ffffff" />
      {/* Texto curvo superior (blanco sobre azul) */}
      <text fill="#ffffff" fontSize="5" fontWeight="700" letterSpacing="0.3">
        <textPath href="#fcpnTop" startOffset="50%" textAnchor="middle">
          FACULTAD DE CIENCIAS PURAS Y NATURALES
        </textPath>
      </text>
      {/* Disciplinas en azul */}
      <text fill="#0B2D5C" fontSize="3" fontWeight="700">
        <textPath href="#fcpnMid" startOffset="50%" textAnchor="middle">
          BIOLOGÍA · QUÍMICAS · FÍSICA · INFORMÁTICA · MATEMÁTICA
        </textPath>
      </text>
      {/* FCPN central */}
      <text
        x="50"
        y="62"
        textAnchor="middle"
        fontSize="22"
        fontWeight="800"
        fill="#0B2D5C"
        fontFamily="Inter, sans-serif"
      >
        FCPN
      </text>
      {/* Pestana inferior */}
      <path
        d="M 22 80 Q 50 88 78 80 L 78 91 Q 50 95 22 91 Z"
        fill="#0B2D5C"
      />
      <text
        x="50"
        y="89"
        textAnchor="middle"
        fontSize="6"
        fontWeight="700"
        fill="#ffffff"
      >
        U.M.S.A.
      </text>
    </svg>
  )
}
