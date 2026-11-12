import { useState } from 'react'
import StatusBadge from './StatusBadge'
import { shutdown } from '../api'

// Logo institucional de la UMSA (mismo que usa chat_gaceta_web.py).
const LOGO_URL =
  'https://www.umsa.bo/image/company_logo?img_id=3107403&t=1773455290863'

// Sidebar responsivo:
//   - Desktop (md+): fijo a la izquierda como columna del flex padre.
//   - Mobile (<md): cajon (drawer) que entra deslizandose desde la izquierda
//     cuando `abierto` es true; oculto fuera de pantalla cuando es false.
export default function Sidebar({
  conversaciones,
  activaId,
  estado,
  abierto,
  onCerrar,
  onNueva,
  onSeleccionar,
}) {
  const [logoError, setLogoError] = useState(false)

  const cerrarSesion = async () => {
    const ok = window.confirm(
      '¿Cerrar sesión?\n\n' +
        'Esto borrará tus conversaciones, detendrá el servidor (Python) ' +
        'y cerrará esta ventana.',
    )
    if (!ok) return

    // Se pide el cierre ANTES de borrar nada de la UI: si el servidor lo
    // rechaza (401, protegido con GACETA_ADMIN_TOKEN) mostramos un aviso
    // limpio y dejamos la sesión intacta en lugar de dejar al usuario
    // viendo "Sesión cerrada" con el servidor todavía corriendo.
    const cerrado = await shutdown()
    if (!cerrado) {
      window.alert(
        'No se pudo cerrar la sesión: el servidor está protegido con un ' +
          'token de administrador. Ciérralo desde donde lo iniciaste.',
      )
      return
    }

    const oscuro = document.documentElement.classList.contains('dark')
    document.body.innerHTML = `
      <div style="font-family:Inter,system-ui,sans-serif;display:flex;
                  align-items:center;justify-content:center;height:100vh;
                  text-align:center;padding:24px;
                  background:${oscuro ? '#000' : '#fff'};
                  color:${oscuro ? '#fff' : '#000'};">
        <div>
          <h1 style="margin:0 0 16px;font-weight:600;font-size:28px;">
            Sesión cerrada
          </h1>
          <p style="opacity:0.7;font-size:15px;max-width:420px;line-height:1.5;">
            El servidor se ha detenido. Puedes cerrar esta pestaña.
          </p>
        </div>
      </div>
    `

    try {
      localStorage.removeItem('gaceta-conversaciones')
    } catch {
      // sin localStorage: nada que borrar
    }

    window.close()
  }

  return (
    <aside
      className={`flex flex-col h-screen w-[300px] shrink-0
                  bg-umsa-blanco dark:bg-black
                  border-r-2 border-black/20 dark:border-white/20
                  fixed inset-y-0 left-0 z-40
                  md:static md:translate-x-0
                  transform transition-transform duration-200
                  ${abierto ? 'translate-x-0' : '-translate-x-full md:translate-x-0'}`}
    >
      {/* Boton cerrar (solo mobile) */}
      <button
        onClick={onCerrar}
        aria-label="Cerrar menú"
        className="md:hidden absolute top-3 right-3 w-9 h-9 rounded-full flex items-center justify-center text-black dark:text-white hover:bg-black/10 dark:hover:bg-white/10 z-10"
      >
        <span className="material-symbols-outlined">close</span>
      </button>

      {/* Cabecera con logo */}
      <div className="p-5 md:p-6 flex items-center gap-4 border-b-2 border-black/20 dark:border-white/20">
        {logoError ? (
          <span className="material-symbols-outlined text-4xl">school</span>
        ) : (
          <img
            alt="UMSA Logo"
            className="h-12 w-auto object-contain"
            src={LOGO_URL}
            onError={() => setLogoError(true)}
          />
        )}
        <div className="flex flex-col">
          <span className="text-xl font-bold tracking-tight uppercase leading-none">
            GACETA IA
          </span>
          <span className="text-xs text-black/70 dark:text-white/70 mt-1 uppercase tracking-wider">
            UMSA
          </span>
        </div>
      </div>

      {/* Estado del motor de IA */}
      <div className="px-5 md:px-6 py-3 border-b border-black/10 dark:border-white/10">
        <StatusBadge estado={estado} />
      </div>

      {/* Navegacion + historial */}
      <nav className="px-5 md:px-6 mt-6 flex flex-col gap-4 flex-1 overflow-y-auto chat-scroll pb-4">
        <button
          onClick={onNueva}
          className="flex items-center gap-3 w-full py-3 px-4 border-2 border-black dark:border-white text-black dark:text-white hover:bg-black/10 dark:hover:bg-white/10 transition-colors"
        >
          <span className="material-symbols-outlined text-lg">add</span>
          <span className="text-left w-full">Nueva Conversación</span>
        </button>

        <div className="mt-4 flex flex-col gap-1">
          <span className="text-black/50 dark:text-white/50 text-xs font-semibold tracking-wider uppercase mb-2">
            Historial
          </span>

          {conversaciones.length === 0 && (
            <span className="text-black/40 dark:text-white/40 text-sm px-3 py-2">
              Aún no hay conversaciones.
            </span>
          )}

          {conversaciones.map((c) => {
            const activo = c.id === activaId
            return (
              <button
                key={c.id}
                onClick={() => onSeleccionar(c.id)}
                title={c.title}
                className={`flex items-center gap-3 w-full py-2 px-3 transition-colors text-left ${
                  activo
                    ? 'bg-black/10 dark:bg-white/10 text-black dark:text-white'
                    : 'text-black/80 dark:text-white/80 hover:text-black dark:hover:text-white hover:bg-black/5 dark:hover:bg-white/5'
                }`}
              >
                <span className="material-symbols-outlined text-sm opacity-70">
                  chat_bubble_outline
                </span>
                <span className="truncate">
                  {c.title || 'Nueva conversación'}
                </span>
              </button>
            )
          })}
        </div>
      </nav>

      {/* Pie: cerrar sesion */}
      <div className="p-5 md:p-6 mt-auto border-t-2 border-black/20 dark:border-white/20">
        <button
          onClick={cerrarSesion}
          className="text-black/80 dark:text-white/80 hover:text-black dark:hover:text-white transition-colors flex items-center w-full py-2"
        >
          <span className="material-symbols-outlined mr-3">logout</span>
          Cerrar Sesión
        </button>
      </div>
    </aside>
  )
}
