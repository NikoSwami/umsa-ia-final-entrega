import { useState } from 'react'
import { useTheme } from './hooks/useTheme'
import { useChat } from './hooks/useChat'
import { useStatus } from './hooks/useStatus'
import { useListas } from './hooks/useListas'
import { useMemoria } from './hooks/useMemoria'
import { useBranding } from './hooks/useBranding'
import Sidebar from './components/Sidebar'
import ThemeToggle from './components/ThemeToggle'
import ListasToggle from './components/ListasToggle'
import MemoriaToggle from './components/MemoriaToggle'
import ChatArea from './components/ChatArea'
import ChatInput from './components/ChatInput'

export default function App() {
  const { theme, toggleTheme } = useTheme()
  const estado = useStatus()
  const listas = useListas()
  const memoria = useMemoria()
  const branding = useBranding()
  const {
    conversaciones,
    activa,
    activaId,
    enviando,
    enviarMensaje,
    nuevaConversacion,
    seleccionarConversacion,
  } = useChat()

  // Estado del cajon lateral en mobile (< 768 px). En desktop el sidebar
  // se ve siempre — este flag solo afecta a pantallas chicas.
  const [sidebarAbierto, setSidebarAbierto] = useState(false)
  const cerrarSidebar = () => setSidebarAbierto(false)
  const abrirSidebar = () => setSidebarAbierto(true)

  // En mobile, al elegir una conversacion (o crear nueva) cerramos el cajon
  // para que el usuario vuelva a ver el chat. En desktop no tiene efecto.
  const onNueva = () => {
    nuevaConversacion()
    cerrarSidebar()
  }
  const onSeleccionar = (id) => {
    seleccionarConversacion(id)
    cerrarSidebar()
  }

  return (
    <div className="bg-umsa-blanco dark:bg-black min-h-screen antialiased flex text-black dark:text-white">
      <Sidebar
        conversaciones={conversaciones}
        activaId={activaId}
        estado={estado}
        abierto={sidebarAbierto}
        onCerrar={cerrarSidebar}
        onNueva={onNueva}
        onSeleccionar={onSeleccionar}
      />

      {/* Backdrop semi-transparente solo en mobile cuando el cajon esta abierto */}
      {sidebarAbierto && (
        <div
          onClick={cerrarSidebar}
          aria-hidden="true"
          className="md:hidden fixed inset-0 bg-black/50 z-30"
        />
      )}

      <main className="flex-1 flex flex-col h-screen relative bg-umsa-blanco dark:bg-black">
        {/* Hamburguesa flotante (≡): solo mobile, solo cuando el sidebar
            esta cerrado. Arriba-izquierda, mismo tamano que el boton de
            tema para consistencia visual. */}
        {!sidebarAbierto && (
          <button
            onClick={abrirSidebar}
            aria-label="Abrir menú"
            title="Abrir menú"
            className="md:hidden absolute top-6 left-6 z-20 p-2 text-black dark:text-white hover:opacity-70 transition-opacity"
          >
            <span className="material-symbols-outlined text-3xl">menu</span>
          </button>
        )}

        <div className="absolute top-6 right-6 z-20 md:top-8 md:right-12 flex flex-col items-end gap-3">
          {listas.soportado && (
            <ListasToggle
              habilitadas={listas.habilitadas}
              cargando={listas.cargando}
              onToggle={listas.toggle}
            />
          )}
          {memoria.soportado && (
            <MemoriaToggle
              habilitada={memoria.habilitada}
              cargando={memoria.cargando}
              onToggle={memoria.toggle}
            />
          )}
          <ThemeToggle theme={theme} onToggle={toggleTheme} />
        </div>

        <ChatArea
          mensajes={activa.messages}
          enviando={enviando}
          branding={branding}
          onSugerencia={enviarMensaje}
          modeloListo={estado.loaded}
        />

        <ChatInput
          onEnviar={enviarMensaje}
          enviando={enviando}
          modeloListo={estado.loaded}
        />
      </main>
    </div>
  )
}
