# Instrucciones de uso

Guía completa para instalar, configurar, construir la base y operar el
asistente de consulta de la Gaceta.

Índice:

1. [Requisitos](#1-requisitos)
2. [Instalación](#2-instalación)
3. [Configuración (`.env`) y conexión con Gemini](#3-configuración-env-y-conexión-con-gemini)
4. [Construir la base de datos](#4-construir-la-base-de-datos)
5. [Arrancar el sistema (local)](#5-arrancar-el-sistema-local)
6. [Modo Docker](#6-modo-docker)
7. [Cargar documentos](#7-cargar-documentos)
8. [Uso de la API (todos los endpoints)](#8-uso-de-la-api-todos-los-endpoints)
9. [Servicio de OCR (opcional)](#9-servicio-de-ocr-opcional)
10. [Solución de problemas](#10-solución-de-problemas)

---

## 1. Requisitos

| Requisito | Detalle |
|---|---|
| **Python 3.13, 64 bits, Windows** | **Obligatorio.** Los componentes del sistema requieren esta versión exacta de Python en Windows de 64 bits. Con otra versión o sistema operativo no funcionará. |
| Node.js 18+ | Solo para compilar la interfaz web (`frontend/`). |
| Modelo de lenguaje | Por **API** (recomendado, p. ej. Google Gemini) o **local** (requiere GPU; ver `requirements-windows.txt`). |
| Docker Desktop | Opcional, solo si se usa el [modo Docker](#6-modo-docker). |
| Espacio en disco | Varios GB para los modelos de *embeddings* y, si se usa, el modelo de lenguaje local. |

Comprobar la versión de Python: `python --version` (debe indicar `3.13.x`, 64 bits).

---

## 2. Instalación

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Para usar un **modelo de lenguaje local** (opcional):

```bash
pip install -r requirements-windows.txt
```

---

## 3. Configuración (`.env`) y conexión con Gemini

Copiar la plantilla:

```bash
copy .env.example .env
```

El `.env` controla todo (y **es el mismo archivo que usa Docker**). Lo más
importante es elegir el **backend del modelo de lenguaje**.

### Conectar con Google Gemini (recomendado)

En el `.env`, definir estas tres variables:

```ini
GACETA_BACKEND=api
GACETA_API_PROVEEDOR=gemini
GEMINI_API_KEY=TU_CLAVE_DE_GEMINI
```

- La clave de Gemini se obtiene en **Google AI Studio** (`https://aistudio.google.com`).
- **Ahí es donde se pone la API para que funcione** — tanto en local como en
  Docker, que lee este mismo `.env` automáticamente.

Otros proveedores compatibles (una sola clave alcanza, con el nombre del
proveedor): `groq`, `openrouter`, `cerebras` — por ejemplo
`GACETA_API_PROVEEDOR=openrouter` + `OPENROUTER_API_KEY=...`.

### Modelo local (alternativa)

Dejar `GACETA_BACKEND` vacío: usa el modelo local Qwen2.5-7B (requiere GPU).

> El sistema **no** trae ninguna clave incluida: cada despliegue pone la suya en
> el `.env`. Ese archivo nunca debe subirse a un repositorio (ya está en
> `.gitignore`).

---

## 4. Construir la base de datos

El asistente responde sobre los documentos que haya en la base. Para construirla:

1. Colocar los documentos fuente (resoluciones en formato JSON) en
   `gaceta_rag/data/raw/`.
2. Construir toda la base con un solo comando:

   ```bash
   python construir_todo.py
   ```

   Genera, en orden: fragmentos → *embeddings* → índice de búsqueda → grafo de
   relaciones → índices de apoyo.

Opciones útiles:

| Comando | Para qué |
|---|---|
| `python construir_todo.py --limit 200` | Prueba rápida con 200 documentos. |
| `python construir_todo.py --incremental` | Solo documentos nuevos o modificados. |
| `python construir_todo.py --solo-base` | Construir sin el grafo de citas. |

> También se pueden cargar documentos de a uno desde la web: ver
> [sección 7](#7-cargar-documentos). Los escaneados necesitan OCR
> ([sección 9](#9-servicio-de-ocr-opcional)).

---

## 5. Arrancar el sistema (local)

**Compilar la interfaz web** (la primera vez y tras cambios en `frontend/`):

```bash
cd frontend
npm install
npm run build
cd ..
```

**Arrancar el servidor** (sirve la API y la interfaz en el mismo puerto):

```bash
python api_gaceta.py --host 127.0.0.1 --port 8000
```

- **El chat queda en http://127.0.0.1:8000** (interfaz web).
- La documentación interactiva de la API está en **http://127.0.0.1:8000/docs**.

> **Lanzador opcional:** `iniciar_web_react.bat` automatiza estos pasos, pero
> espera un entorno virtual en `venv\` y Node.js portátil en `node\`. Con una
> instalación normal, conviene usar los comandos de arriba.

---

## 6. Modo Docker

Alternativa a la instalación local: levanta todo en contenedores. Requiere
**Docker Desktop**.

**Pasos (modo API, el recomendado):**

```bash
copy .env.example .env
#  en .env: GACETA_BACKEND=api, GACETA_API_PROVEEDOR=gemini, GEMINI_API_KEY=...
docker compose up -d --build
```

Docker lee la `.env` automáticamente, así que **la clave de Gemini va en ese
`.env`** (sección 3) y con eso el contenedor queda conectado al modelo.

**Rutas y puertos (modo API):**

| URL | Qué es |
|---|---|
| http://127.0.0.1:8000 | **Chat** (interfaz web) |
| http://127.0.0.1:8000/carga | **Carga manual** de documentos — el token de acceso aparece en `docker compose logs gaceta` |
| http://127.0.0.1:8000/carga-mof | Subir el respaldo (`pg_dump`) de MOF — mismo token |
| http://127.0.0.1:8000/docs | Documentación interactiva de la API |

Servicios que levanta: `gaceta` (publicado en el puerto 8000) y `ocr` (interno,
puerto 8001, no se publica). La primera vez descarga los modelos de
*embeddings* y reranker.

**Variantes:**

```bash
# OCR por GPU (escaneados pesados)
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build

# Entrada de documentos por MCP (agrega Paperless)
docker compose -f docker-compose.yml -f docker-compose.paperless.yml up -d --build
```

El compose de Paperless agrega estos servicios (sus puertos y contraseñas se
configuran en el `.env`: `PAPERLESS_SECRET_KEY`, `PAPERLESS_ADMIN_PASSWORD`,
`PAPERLESS_MCP_API_KEY`):

| URL | Qué es |
|---|---|
| http://127.0.0.1:8002 | Paperless-ngx (gestor documental) |
| http://127.0.0.1:3100 | Servidor MCP de Paperless |

> Nota: la imagen se construye sin corpus; la base arranca vacía y se llena
> construyéndola (sección 4) o cargando documentos (sección 7).

---

## 7. Cargar documentos

Hay tres formas de incorporar documentos:

1. **Desde la web (manual):** abrir **http://127.0.0.1:8000/carga**, subir PDFs
   o pegar texto. Requiere el **token de administrador** (`GACETA_ADMIN_TOKEN` o
   `GACETA_CARGA_TOKEN` del `.env`; en Docker aparece en
   `docker compose logs gaceta`).
2. **Masivo (archivos JSON):** colocar las resoluciones en
   `gaceta_rag/data/raw/` y correr `python construir_todo.py` (sección 4).
3. **Por MCP (Paperless):** guía interactiva en **http://127.0.0.1:8000/guia-mcp**;
   requiere el compose de Paperless (sección 6).

La página web `/carga` hace OCR si hace falta, deja revisar el texto extraído y
confirmar antes de indexar. Los mismos pasos están disponibles por API
(`POST /api/carga/archivo`, `POST /api/carga/texto`, seguimiento en
`/api/carga/trabajos`).

---

## 8. Uso de la API (todos los endpoints)

Todo corre en **un único servidor FastAPI en el puerto 8000**. La documentación
interactiva completa (probar cada endpoint desde el navegador) está en
**http://127.0.0.1:8000/docs**.

Los endpoints de administración y carga exigen el header `X-Admin-Token` con el
token del `.env`.

**Chat y consulta**

| Método y ruta | Para qué |
|---|---|
| `GET /` | Interfaz web (el **chat**). |
| `POST /api/chat` | Enviar una pregunta y recibir la respuesta con sus citas. |
| `GET /api/status` | Estado del sistema (si el modelo está listo). |
| `GET /api/sugerencias` | Preguntas sugeridas de ejemplo. |
| `POST /api/nueva` | Iniciar una conversación nueva (admin). |

**Carga de documentos** (ver sección 7)

| Método y ruta | Para qué |
|---|---|
| `GET /carga` | Página web de carga manual. |
| `POST /api/carga/archivo` · `POST /api/carga/texto` | Subir un PDF o cargar texto. |
| `GET /api/carga/trabajos` · `.../{id}` · `.../{id}/ocr` | Seguir el procesamiento. |
| `POST /api/carga/trabajos/{id}/confirmar` | Confirmar e indexar. |
| `GET/PUT/DELETE /api/carga/documentos/{id}` | Consultar, editar o borrar documentos. |
| `POST /api/carga/grafo` | Reconstruir el grafo de citas. |

**Organigrama / MOF**

| Método y ruta | Para qué |
|---|---|
| `GET /api/mof/organigrama` | Consultar el organigrama. |
| `GET/POST /api/ingesta/mof` · `POST /api/reconciliar/mof` | Sincronizar con MOF. |
| `GET /carga-mof` · `POST /api/mof/respaldo` | Subir el respaldo (`pg_dump`) de MOF. |

**Ingesta por MCP (Paperless)**

| Método y ruta | Para qué |
|---|---|
| `POST /api/paperless/evento` | Webhook de Paperless (documento nuevo). |
| `GET /api/paperless/estado` · `POST /api/paperless/reconciliar` | Estado y resincronización. |
| `GET /guia-mcp` · `GET /api/mcp/guia` · `POST /api/mcp/revisar` | Guía y verificación de la carga por MCP. |

Ejemplo de consulta al chat con `curl`:

```bash
curl -X POST http://127.0.0.1:8000/api/chat ^
  -H "Content-Type: application/json" ^
  -d "{\"mensaje\": \"¿Qué resoluciones mencionan la modalidad de graduación?\"}"
```

La respuesta incluye el texto redactado y las referencias a los documentos que
lo sustentan.

---

## 9. Servicio de OCR (opcional)

Para incorporar documentos **escaneados** (imágenes sin capa de texto), la
carpeta `servicio_ocr/` contiene un microservicio independiente (su README
explica la instalación). En modo Docker, el servicio `ocr` ya se levanta solo.
Solo es necesario si se cargan documentos sin texto digital.

---

## 10. Solución de problemas

| Síntoma | Causa probable y solución |
|---|---|
| Error al cargar un componente del sistema al arrancar | La versión de Python no es 3.13 de 64 bits en Windows. Verificar con `python --version`. |
| Error de clave o de proveedor del modelo | Falta configurar el backend en `.env`: `GACETA_BACKEND=api`, `GACETA_API_PROVEEDOR=gemini`, `GEMINI_API_KEY=...` (sección 3). En Docker, reiniciar con `docker compose up -d` tras editar el `.env`. |
| "La base está vacía" o respuestas sin fuentes | No se construyó la base: `python construir_todo.py` (sección 4) o cargar documentos (sección 7). |
| El servidor arranca pero la primera respuesta tarda | Normal: los modelos se cargan en la primera consulta. |
| La interfaz no carga | Falta compilar el frontend: `cd frontend && npm install && npm run build`. |
| `/carga` pide un token | Es el `GACETA_ADMIN_TOKEN`/`GACETA_CARGA_TOKEN` del `.env`; en Docker sale en `docker compose logs gaceta`. |
