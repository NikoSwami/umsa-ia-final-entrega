import typography from '@tailwindcss/typography'

/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        'system-glow': '#4FB2C1',
        // Blanco institucional suave de umsa.bo (#F5F5F5) para el lienzo de
        // fondo, en lugar del blanco puro (#FFFFFF) que resultaba agresivo.
        // Las tarjetas/mensajes siguen en blanco puro para contrastar suave.
        'umsa-blanco': '#f5f5f5',
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
      },
    },
  },
  plugins: [typography],
}
