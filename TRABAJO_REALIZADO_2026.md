# Trabajo realizado — 2026

Registro histórico del desarrollo del asistente de consulta de la Gaceta (RAG
sobre resoluciones de la UMSA). Resume lo que se implementó, lo que se probó y
midió, y lo que se descartó (con el porqué).

> Registro del recorrido de desarrollo del proyecto durante 2026: lo
> implementado, lo probado y lo descartado.

---

## 1. Panorama general

- **Período:** febrero → noviembre de 2026.
- **Recorrido:** el proyecto se desarrolló a lo largo de sucesivas iteraciones
  de trabajo — de la carpeta **M 4 a la M 9** y luego de la **G 1 a la G 8**.
- **Corpus:** ~3.235 resoluciones.
- **Stack:** Python + FastAPI + React; RAG con búsqueda híbrida (FAISS + FTS),
  reranker BGE y un modelo de lenguaje (Qwen 2.5 7B local por GPU, o por API —
  Gemini/otros).
- **Disciplina de calidad:** cada cambio se midió contra una línea base y no se
  aceptó si bajaba (ver sección 2).

Etapas del trabajo, en orden:

| Etapa | Foco |
|---|---|
| 1 | Núcleo RAG; corrección de búsqueda por persona; arreglo del OCR; filtro de firmantes. |
| 2 | Base incremental real; títulos desde el `raw`; backend por API (modelo gratis); notebooks de Colab. |
| 3 | Integración con MOF (organigrama) por fases; endurecimiento para servidor; autenticación. |
| 4 | Carga manual de documentos (`/carga`) + OCR; Docker modo API; carga del respaldo de MOF. |
| 5 | Entrada de documentos por Paperless + MCP; guía de uso. |

---

## 2. Método de trabajo y línea base

Regla fija durante todo el proyecto: **medir antes de tocar, y no dejar que la
calidad baje.**

- **Pruebas automáticas (pytest):** crecieron de ~851 a ~999 a lo largo del
  proyecto. Verde obligatorio antes de cerrar cualquier cambio.
- **Preguntas "doradas" (`eval_doradas.py`):** 48 casos (personas / temas /
  vigencia / vigencia+tema). El gate era **48/48**; si una corrida bajaba, se
  revertía el cambio.
- **Evaluación del organigrama (`eval_organigrama.py`):** 20 preguntas contra el
  motor real. Fue de **1/20** (línea base) a **20/20** al cerrar la integración
  con MOF.
- **Principio sostenido:** toda cifra o identificador que el usuario ve debe
  salir de código determinista o de la fuente, nunca "recordado" por el modelo.

---

## 3. Lo implementado

### 3.1 Núcleo RAG y calidad de las respuestas

- Pipeline de búsqueda híbrida (vectorial + léxica), reranker y redacción con
  citas a las fuentes.
- **Búsqueda por persona precisa:** se corrigió un falso positivo real — un
  listado por nombre devolvía resoluciones de homónimos (apellidos frecuentes
  como López/Flores/Ramírez). Se exigió que el nombre apareciera como **frase**,
  no como tokens sueltos, y se unificó la ruta para que la misma persona dé
  siempre la misma lista sin importar cómo se formule la pregunta.
- **"Quién es X" sin alucinar el rol:** antes solo se mandaba el primer fragmento
  de cada resolución al modelo, y si la mención de la persona caía más adelante,
  el modelo inventaba su rol. Se pasó a texto completo priorizando el fragmento
  donde el nombre realmente aparece.
- **Filtro de firmantes/remitentes:** un `personas_firmantes.json` marca los
  documentos donde la persona solo firma o tramita (no es el sujeto). Hallazgo
  clave: una persona con 536 documentos resultó ser remitente institucional en
  casi todos; con el filtro quedó en 149. Con salvaguardas deliberadas (umbral
  de documentos, protección a renuncias y a quien pide para sí).

### 3.2 OCR y datos

- **Arreglo del marcador "N°":** el OCR original corrompía el símbolo del número
  de resolución ("N°" → "NC", "N*", "N?"…) en ~2.750 de 3.235 documentos. Se
  corrigió con una regex conservadora, aplicada a los datos ya cargados y
  metida en el pipeline para que un rebuild no lo reintroduzca.
- **Títulos desde el `raw`:** la base de títulos estaba ignorada por git, así
  que en cualquier clon limpio las citas caían al membrete del OCR. Se pasó a
  leer el título del `raw` versionado.
- **Base incremental de verdad:** el modo "incremental" ahora salta los
  documentos ya procesados y recicla los vectores cuyo texto no cambió.

### 3.3 Integración con MOF (organigrama) — el trabajo más grande

Objetivo: que el chat responda sobre el organigrama (unidades, dependencias,
cargos, funciones) con datos exactos, sin que el modelo invente cifras. Se hizo
**por fases**, con reglas fijas (la Gaceta solo lee MOF; todo número sale de la
réplica; el esquema se versiona; prueba antes de tocar):

| Fase | Qué agregó | Resultado |
|---|---|---|
| 0 | Línea base del organigrama | 1/20 |
| 1 | Detección de intención (qué pide la consulta) | 20/20 detectadas, 0 falsos positivos en las doradas |
| 2 | Contar / listar / jerarquía desde la réplica, sin LLM | 9/20 |
| 3 | Bloque de hechos verificados delante del modelo | 18/20 |
| 4 | Verificación: una cifra fuera del bloque descarta la redacción | 18/20, 0 descartes |
| 5 | El motor por API responde el organigrama igual que el local | (paridad verificada) |
| 6 | Cada respuesta dice de cuándo son los datos; con MOF caído responde igual | — |

Tras importar la **réplica del MOF real**: **20/20**.

Diseño elegido por el usuario: **la Gaceta le pregunta a MOF qué cambió** (cada
30 s lee la auditoría y relee solo lo que cambió); MOF no empuja. La Gaceta
guarda el organigrama en dos formas: fichas de texto (para buscar por
significado) y una base estructurada (para contar y listar exacto).

### 3.4 Despliegue: Docker y backend por API

- **Backend por API (modelo gratis):** además del Qwen local por GPU, el sistema
  habla con proveedores por HTTP (Gemini y otros). Se resolvieron varios
  problemas reales de Gemini (respuestas vacías por "thinking tokens", 429/403,
  presupuesto de tokens).
- **Docker modo API:** el mismo sistema que corría en Colab, empaquetado en
  contenedores para correr local (`docker-compose.yml`), con el servicio de OCR
  aparte.
- **Notebooks de Colab** para correr la app y los evals con GPU gratis.

### 3.5 Carga de documentos

- **Carga manual por web (`/carga`):** subir PDFs (con OCR), texto o JSON,
  revisar lo extraído y confirmar antes de indexar.
- **Carga del respaldo de MOF (`/carga-mof`):** subir el `pg_dump` de MOF desde
  la web, con informe de lo que se eliminó y válvula de seguridad si se borra
  demasiado.
- **Entrada por Paperless + MCP:** Paperless-ngx como puerta de
  entrada de documentos, con un servidor MCP, webhook hacia la carga, y
  reconciliación periódica (porque un webhook siempre pierde alguno). Guía de
  uso en `/guia-mcp`.

---

## 4. Lo que se probó y midió

- **Punta a punta de la carga/Docker:** build OK, Gemini
  respondiendo, OCR funcionando, carga + verificación + grafo OK. Con marcas
  únicas por documento para probar que el texto llegaba al contexto del modelo.
- **Benchmark de OCR:** se midió calidad de tablas y tiempo/memoria por página
  entre los motores (ver sección 5).
- **Evals del organigrama con el motor real** (Qwen local en GPU de 6 GB,
  ~16–18 min por corrida), incluida la corrida final 20/20.
- **Pruebas de la carga del respaldo de MOF:** 22 pruebas, cada una verificada
  forzando una mutación; ciclo real probado en Docker.

---

## 5. Lo que se desarrolló y se descartó (y por qué)

Esta es la parte que más suele perderse: cosas que se construyeron o evaluaron
y **no** quedaron.

### 5.1 Resumen enriquecido por persona — **descartado**

Se construyó y **se verificó correcto en vivo** una ruta que, además de listar
las resoluciones de una persona, daba un resumen y detalle de cada una (con los
números de resolución saliendo siempre de código determinista para que el modelo
no los mezclara ni inventara). Aun así, el usuario decidió volver al **listado
pelado** (determinista, sin LLM). El trabajo no se perdió: quedó archivado en la
rama `archivo/resumen-persona-descartado` y se revirtió en `main`. Si se retoma
la idea, la implementación ya está lista.

### 5.2 OCR PP-StructureV3 (PaddleOCR estructural) — **descartado**

Se evaluó para resolver tablas. Con Paddle 3.3.1 en CPU se caía (error de
oneDNN en los detectores de layout); con 3.2.2 funcionaba pero a ~113 s por
página y **tampoco** resolvía las tablas sin bordes. Se abandonó.

### 5.3 OCR PaddleOCR-VL como motor por defecto — **reemplazado**

El motor VL daba buena calidad pero era **inviable por defecto**: 396–779 s y
~8,5 GB por página en CPU. Se desarrolló un **motor liviano en ONNX** (RapidOCR
PP-OCRv5 + layout + tablas) que da **la misma calidad de tablas** a 5–7 s y
0,8 GB por página. El liviano quedó como predeterminado; VL se conservó como
opción (`OCR_MOTOR=vl`), pensado para GPU.

### 5.4 Réplica del organigrama del "seeder" de pruebas — **reemplazada**

Durante el desarrollo, el organigrama se probó contra una réplica de datos de
prueba. Se reemplazó por la **réplica del MOF real** (importada de un
`pg_dump`), que es la que quedó. Las mediciones viejas dejaron de ser
comparables y las preguntas del eval se regeneraron.

### 5.5 Que MOF empuje los cambios a la Gaceta — **no se usó**

Existía (y se mergeó) un servicio en MOF para empujar cambios hacia la Gaceta.
Por decisión explícita del usuario — «mi chat bot es el que tiene que hacer la
consulta» — se dejó **sin usar**: es la Gaceta la que pregunta. La parte de
auditoría de ese trabajo sí se aprovechó.

### 5.6 Plan de "8 prompts" para el organigrama — **superado**

Había un plan inicial de resolver el organigrama con una secuencia de prompts.
Se descartó en favor de la implementación **por fases con verificación**
(sección 3.3), que da cifras exactas en vez de depender del modelo.

---

## 6. Límites conocidos y decisiones conscientes

No todo se "arregló": algunas cosas se aceptaron a propósito.

- **Fiabilidad del modelo pequeño:** Qwen 2.5 7B cuantizado no es 100 % confiable
  atribuyendo identificadores o roles exactos cuando hay varios candidatos
  parecidos a la vez en el contexto. Donde importaba (números de resolución,
  cifras del organigrama) se blindó con código determinista; el riesgo residual
  en redacciones libres ("quién es X") se **aceptó conscientemente** en vez de
  perseguirlo caso por caso con más prompt engineering (ya se probó que no
  escala con este modelo).
- **~47 casos de OCR "N°"** con formatos raros quedaron sin corregir a propósito,
  porque arreglarlos arriesgaba alterar dígitos reales del número o el año.
- **Fuente del organigrama:** el sistema responde con los datos de la réplica y
  avisa de cuándo son; si MOF está caído, responde igual con la última foto.

---

*Documento de registro histórico del proyecto, desarrollado entre febrero y
noviembre de 2026 a lo largo de las iteraciones M 4–M 9 y G 1–G 8.*
