# Servicio OCR

El OCR de la carga manual de la Gaceta (`/carga`), detrás de una API FastAPI.
La Gaceta le pasa los PDFs escaneados y las imágenes por HTTP (`OCR_URL`).

Dos motores, con la misma API (`OCR_MOTOR`):

| | `liviano` (por defecto) | `vl` |
|---|---|---|
| Qué es | PP-OCRv5 latino + PP-DocLayoutV3 + SLANet+, en ONNX ([RapidOCR](https://github.com/RapidAI/RapidOCR)) | PaddleOCR-VL-1.5, el de PaddleOCR-VL |
| Código | `gaceta_rag/carga/ocr_liviano.py` | `gaceta_rag/carga/ocr.py` (`MotorOCRPaddle`) |
| Instalación | ~300 MB + ~145 MB de modelos | ~3 GB + ~2 GB de modelos |
| Cuándo | Siempre, salvo que haya GPU y se quiera el VL | Con GPU |

Va en un proceso aparte a propósito: las dependencias del OCR no tienen nada
que ver con el chat, y el VL ocupa ~8 GB mientras lee.

## Lo que se midió (CPU, esta laptop)

Cuatro páginas "escaneadas" con verdad conocida: una resolución real (texto,
acentos, `N°`); un anexo con una tabla **con bordes** y otra **sin bordes**; la
tabla real de costos del proyecto (8 columnas); y un anexo **difícil**: encabezado
de dos niveles con **celdas combinadas** ("Costo (Bs.)" sobre "Matrícula" y
"Mensualidad"), celdas de **varios renglones**, **celdas vacías** y una tabla sin
bordes con una celda partida en dos renglones.

| | `liviano` | `vl` |
|---|---|---|
| Carga del modelo | **3-5 s** | 95-110 s (la primera vez, ~6 min con la descarga) |
| Por página | **5-7 s** (laptop libre) | 396-779 s |
| Memoria (pico) | **0,8 GB** | 8,5 GB |
| Tabla con bordes (24 celdas) | **24/24** | 22/24 |
| Tabla sin bordes (20 celdas) | 20/20 | 20/20 |
| Tabla real de costos (56 celdas) | 56/56 | 56/56 |
| Encabezado de dos niveles + varios renglones (24 celdas) | 23/24 | 23/24 |
| Sin bordes con celda partida (20 celdas) | 20/20 | 20/20 |
| **Total** | **143/144** | 141/144 |
| Texto de la resolución | 100%, acentos 16/16, `N°` ✔ | 100%, 16/16, `N°` ✔ |

"Celdas" = las de la verdad que aparecen en su fila y en su columna exacta. Lo
que falla: al VL se le cae la tilde de "Maestría" (tres veces); el liviano no lee
un guion "—" suelto en una celda (queda vacía).

Por HTTP, una carga completa (subir → OCR → revisión con título sugerido →
indexar y verificar) de un escaneado de una página tarda 4-6 s con el
liviano. Con GPU el VL debería acortar mucho su tiempo; no se midió.

Cómo llega el liviano a ese detalle (cada punto salió de una falla medida):

- **Celdas combinadas**: SLANet+ devuelve `rowspan`/`colspan` bien; el paso a
  Markdown deja la celda vacía debajo (rowspan) o a la derecha (colspan) para
  que el resto de la fila quede en su columna.
- **Números sueltos**: el reconocedor latino confunde dígitos aislados ("34" →
  "$", "9" → "6"). Dentro de las tablas, lo corto o dudoso se relee ampliado con
  PP-OCRv6 small. Fuera de las tablas no: PP-OCRv6 pierde espacios en texto
  corrido.
- **Celdas de varios renglones** sin bordes: un renglón pegado al anterior y con
  casi todas las columnas vacías es la continuación de la misma fila.

Las tablas **sin bordes** son el punto débil de SLANet+ (y de PP-StructureV3,
que usa el mismo modelo): junta columnas. El motor liviano lo resuelve así: si
la región de la tabla no tiene líneas verticales, manda la grilla armada con la
posición de cada palabra (filas por altura, columnas por los huecos
verticales), salvo que SLANet+ vea más columnas. Las tablas con bordes las
resuelve SLANet+.

Se descartó **PP-StructureV3** (PaddleOCR clásico + tablas): con Paddle 3.3.1
en CPU falla (oneDNN no soporta una operación del detector de diseño; sin
oneDNN, ~63 s y 10 GB por página, y se cae con tablas). Con Paddle 3.2.2 anda,
pero tarda ~113 s y 2,7 GB por página y tampoco resuelve las tablas sin bordes.

## Con Docker

Lo levanta `docker compose up -d` (servicio `ocr`, motor liviano; sus modelos
se bajan al construir la imagen). Con GPU y el motor VL:
`docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build`.
Ver `DOCKER.md`.

## En Windows, sin Docker

```powershell
# Instalar (una vez). Liviano en servicio_ocr\.venv; con -Vl, el VL en .venv-vl
servicio_ocr\instalar.ps1

# Arrancar (desde la raíz del repo)
servicio_ocr\.venv\Scripts\python.exe -m uvicorn servicio_ocr.app:app --host 127.0.0.1 --port 8001

# y en la Gaceta:
$env:OCR_URL = "http://127.0.0.1:8001"
```

Las rutas de los entornos son cortas a propósito: Paddle trae archivos con
rutas muy largas y en una carpeta profunda la instalación falla si Windows no
tiene habilitadas las rutas largas.

## API

| | |
|---|---|
| `GET /salud` | `{listo, cargando, error, modelo, dispositivo, cola}` |
| `POST /ocr` (multipart `archivo`, `paginas_max`) | `202 {id}`: queda en cola |
| `GET /ocr/{id}` | `{estado, pagina, paginas, resultado, error}` |
| `DELETE /ocr/{id}` | libera el resultado |
| `POST /ocr/sincrono` | espera y devuelve el resultado (para probar con curl) |

```powershell
curl -F "archivo=@resolucion.pdf" http://127.0.0.1:8001/ocr/sincrono
```

`resultado` = `{markdown, paginas, paginas_leidas, segundos, filas_tabla, modelo}`.
El Markdown ya viene limpio: tablas de pipes, sin HTML ni imágenes.

## Variables

| Variable | Por defecto | |
|---|---|---|
| `OCR_MOTOR` | `liviano` | `liviano` o `vl` |
| `OCR_DPI` | `auto` | liviano: la resolución del propio escaneo (o un número fijo) |
| `OCR_DPI_MIN` | `150` | liviano: piso de esa resolución |
| `OCR_REC` | `v5` | liviano: reconocedor principal (`v5` latino; `v6` pierde espacios en texto corrido) |
| `OCR_VERSION` | `v1.5` | versión del pipeline VL (`v1`, `v1.5`, `v1.6`) |
| `OCR_DISPOSITIVO` | automático | `cpu` o `gpu` (VL) |
| `OCR_PRECARGAR` | `1` | cargar el modelo al arrancar (si no, con el primer PDF) |
| `OCR_MAX_MB` | `200` | tamaño máximo por archivo |

## Trampas que costó encontrar

- **VL: cargar y usar el modelo en el mismo hilo.** Paddle guarda su modo de
  ejecución por hilo; cargado en uno y usado en otro, falla con
  `int(Tensor) is not supported in static graph mode`. El servicio carga el
  modelo dentro del hilo que después lee.
- **Liviano: la detección a 736 px.** Es el valor por defecto de RapidOCR;
  una hoja entera se achica tanto que dos renglones juntos caben en una caja y
  salen como basura. Se detecta a 1280 px y con menos expansión de caja.
- **Liviano: los modelos se bajan dentro del paquete.** En Docker se bajan al
  construir la imagen; al arrancar, el usuario sin privilegios no podría
  escribirlos.
