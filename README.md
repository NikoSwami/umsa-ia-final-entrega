# Asistente de consulta de la Gaceta (RAG)

Asistente de inteligencia artificial que responde preguntas en lenguaje natural
sobre un corpus de resoluciones de la Gaceta Universitaria de la UMSA. Combina
búsqueda híbrida (vectorial + léxica), reordenamiento por relevancia y un modelo
de lenguaje para redactar respuestas citando las fuentes, todo accesible desde
una interfaz web.

> Para la guía paso a paso de instalación, configuración y uso, ver
> **[INSTRUCCIONES_DE_USO.md](INSTRUCCIONES_DE_USO.md)**.

---

## Componentes

| Componente | Qué hace |
|---|---|
| **`Magenta0309-4B`** | Modelo requerido por el sistema, esencial para el funcionamiento (igual que los *embeddings* y el grafo de citas). Incluido en el repositorio. |
| `api_gaceta.py` | Servidor web (FastAPI). Es el punto de entrada del sistema: expone la API y sirve la interfaz. |
| `frontend/` | Interfaz web (React + Vite + Tailwind). |
| `gaceta_rag/` | Carga de documentos, capa de texto/OCR, construcción de índices y utilidades de apoyo. |
| `construir_todo.py` | Construye toda la base de datos del asistente (fragmentos, *embeddings*, índice de búsqueda y grafo) con un solo comando. |
| `servicio_ocr/` | Microservicio de OCR para documentos escaneados (opcional). |
| `docker/`, `docker-compose*.yml` | Despliegue con contenedores (opcional). |

Además del índice de búsqueda y el grafo de citas que se generan al construir la
base, el sistema usa el modelo **`Magenta0309-4B`** (incluido en el repositorio).
Todos estos componentes son esenciales para el funcionamiento.

---

## Requisitos

- **Python 3.13 (64 bits) en Windows.** Requerido por los componentes del sistema.
- **Node.js 18 o superior** (solo para compilar la interfaz web).
- Conexión a un modelo de lenguaje: por **API** (recomendado) o un modelo
  **local** (requiere GPU para un rendimiento razonable).

---

## Puesta en marcha rápida

```bash
# 1) Entorno e instalación de dependencias
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

# 2) Configuración
copy .env.example .env
#   editar .env y definir el proveedor del modelo de lenguaje

# 3) Construir la base (tras colocar los documentos fuente en gaceta_rag/data/raw/)
python construir_todo.py

# 4) Compilar la interfaz y arrancar el servidor
cd frontend && npm install && npm run build && cd ..
python api_gaceta.py --host 127.0.0.1 --port 8000
```

Luego abrir **http://127.0.0.1:8000** en el navegador (el chat). La
documentación interactiva de la API queda en **http://127.0.0.1:8000/docs**.

### Con Docker (alternativa)

```bash
copy .env.example .env    # definir GACETA_API_PROVEEDOR=gemini y GEMINI_API_KEY
docker compose up -d --build
```

Chat en `http://127.0.0.1:8000`, carga de documentos en `/carga`.

---

En **[INSTRUCCIONES_DE_USO.md](INSTRUCCIONES_DE_USO.md)** está el detalle de todo:
todas las APIs y endpoints, el chat, la carga de documentos, las rutas de Docker
y dónde poner la clave de Gemini para conectarse al modelo.
