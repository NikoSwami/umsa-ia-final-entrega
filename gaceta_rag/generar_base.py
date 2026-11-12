"""
MAIN3 — Pipeline incremental v3 (chunks + embeddings nuevos sin reventar RAM)
==============================================================================
Mismo flujo que main.py (lee de data/raw/ -> chunks -> indexa) pero con:

  - Modelo de embeddings NUEVO: intfloat/multilingual-e5-base (768d)
    en lugar del MiniLM-L12 (384d) actual
  - Chunks mas pequenos (~600 chars) con cabecera enriquecida
  - Streaming: 1 doc por vez, NUNCA acumula todo en RAM
  - Print por CADA documento (sabes exactamente donde fallo si crashea)
  - Filtros defensivos: skip docs con OCR > 3 MB, lineas patologicas
  - Logs separados:
      * data/main3_progreso.log    — cada doc procesado
      * data/main3_skipped.log     — docs saltados con razon
  - Pipeline en 2 fases bien separadas:
      FASE A: chunking -> SQLite (rapida, sin modelo cargado)
      FASE B: embeddings -> FAISS (lenta, con modelo cargado solo en esta fase)
  - Incremental por HASH: con --reanudar solo se re-chunkean los documentos
    NUEVOS o cuyo OCR CAMBIO (huella sha1 del contenido en la tabla docs_meta);
    los demas se saltan. Un doc cambiado se re-chunkea borrando antes sus
    chunks viejos, para no dejar duplicados en el indice FTS5.
  - Resumible: si interrumpes con Ctrl+C en FASE A, los chunks ya escritos en
    SQLite quedan; reanudas con --solo-embed
  - Incremental en FASE B: se reciclan del indice anterior los vectores cuyo
    texto sea identico (huella en faiss_metadata_v2.json) y solo se calculan
    los que faltan. --reset o --reembeber-todo fuerzan el recalculo completo.

NO TOCA nada de v1 (faiss_index.bin, gaceta.db, etc).

Uso tipico:
    # primero prueba con 100 docs:
    python gaceta_rag/main3.py --limit 100

    # si va bien, full run:
    python gaceta_rag/main3.py

    # ya hiciste chunks; solo embebe:
    python gaceta_rag/main3.py --solo-embed

    # cambiar modelo:
    python gaceta_rag/main3.py --modelo e5-small   # 384d, mas rapido
    python gaceta_rag/main3.py --modelo e5-large   # 1024d, mejor
"""

import os
import sys
import json
import time
import sqlite3
import argparse
import hashlib
import re
import gc
from pathlib import Path
from datetime import datetime

os.environ["HF_HUB_DISABLE_XET"]    = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

try:
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
except Exception:
    pass

RAIZ              = Path(__file__).parent
CARPETA_DATA      = RAIZ / "data"
CARPETA_RAW       = CARPETA_DATA / "raw"

RUTA_FAISS        = CARPETA_DATA / "faiss_index_v2.bin"
RUTA_META         = CARPETA_DATA / "faiss_metadata_v2.json"
RUTA_SQLITE       = CARPETA_DATA / "chunks_v2.sqlite"
RUTA_CONFIG       = CARPETA_DATA / "embedding_config_v2.json"

RUTA_LOG_PROG     = CARPETA_DATA / "main3_progreso.log"
RUTA_LOG_SKIP     = CARPETA_DATA / "main3_skipped.log"

MODELOS = {
    "e5-small": ("intfloat/multilingual-e5-small",  "~470 MB",  384),
    "e5-base":  ("intfloat/multilingual-e5-base",   "~1.1 GB",  768),
    "e5-large": ("intfloat/multilingual-e5-large",  "~2.2 GB", 1024),
    "bge-m3":   ("BAAI/bge-m3",                     "~2.3 GB", 1024),
}
MODELO_DEFAULT = "e5-base"

CHUNK_SIZE        = 600
CHUNK_OVERLAP     = 120
CHUNK_MIN_LEN     = 50
MAX_CHUNKS_DOC    = 2000

MAX_OCR_MB_DEFAULT = 3.0
MAX_LINE_CHARS     = 50_000

COMMIT_CADA_DOCS   = 50
EMBED_BATCH_SIZE   = 16

def log(msg, flush=True):
    """Print con flush garantizado."""
    print(msg, flush=flush)

def banner(s, char="="):
    log("")
    log(char * 70)
    log(f"  {s}")
    log(char * 70)

_RE_RUIDO    = re.compile(r'[^a-zA-ZáéíóúÁÉÍÓÚñÑüÜ0-9]')
_RE_ESPACIOS = re.compile(r' {3,}')
_RE_NEWLINES = re.compile(r'\n{3,}')

_RE_MARCADOR_N = re.compile(
    r'(esoluci[oó]n\s+)N([^\dNOo°º\s])\s*(?=\d{1,5}\s*/\s*\d{4})',
    re.IGNORECASE,
)

def arreglar_marcador_resolucion(texto: str) -> str:
    """Normaliza el marcador de numero de resolucion mal leido por el OCR
    (ej. 'Resolucion N* 849/2024' o 'RESOLUCION N’ 0829/2025') a 'N° '.
    No toca 'No'/'Nro' (abreviaturas validas) ni deja de tocar el numero."""
    if not texto:
        return texto
    return _RE_MARCADOR_N.sub(lambda m: f"{m.group(1)}N° ", texto)

def limpiar_ocr_seguro(texto: str) -> str:
    """
    Limpia OCR de forma segura: trocea lineas patologicamente largas,
    cuenta alfanumericos sin crear strings enormes.
    """
    if not texto:
        return ""
    texto = arreglar_marcador_resolucion(texto)
    texto = texto.replace("\r\n", "\n").replace("\r", "\n")

    salida = []
    for linea_raw in texto.split("\n"):
        sub_lineas = (
            [linea_raw[i:i + MAX_LINE_CHARS]
             for i in range(0, len(linea_raw), MAX_LINE_CHARS)]
            if len(linea_raw) > MAX_LINE_CHARS
            else [linea_raw]
        )
        for linea in sub_lineas:
            linea = linea.strip()
            if not linea:
                continue
            if len(linea) > 500:
                muestra = linea[:200] + linea[-200:]
                count = sum(1 for ch in muestra if ch.isalnum())
                if count < 6:
                    continue
            else:
                if len(_RE_RUIDO.sub('', linea)) < 3:
                    continue
            linea = _RE_ESPACIOS.sub('  ', linea)
            salida.append(linea)

    return _RE_NEWLINES.sub('\n\n', "\n".join(salida)).strip()

def cabecera_enriquecida(doc: dict) -> str:
    """Cabecera de metadata + frase narrativa para anadir a cada chunk."""
    titulo = (doc.get("titulo") or "").strip()[:200]
    tipo   = (doc.get("tipo_documento") or "").strip()
    etiquetas = doc.get("etiquetas") or []
    pid    = doc.get("paperlessId") or doc.get("id") or "?"
    paginas = doc.get("nroPaginas") or 0

    partes = []
    if titulo:    partes.append(f"DOCUMENTO: {titulo}")
    if tipo:      partes.append(f"TIPO: {tipo}")
    if etiquetas:
        et_str = ", ".join(str(e) for e in etiquetas)[:200]
        partes.append(f"ETIQUETAS: {et_str}")
    partes.append(f"ID: {pid}")
    if paginas:   partes.append(f"PAGINAS: {paginas}")

    cabecera = "[" + " | ".join(partes) + "]"

    if titulo and tipo:
        narrativa = f"Fragmento de la {tipo.lower()} \"{titulo[:80]}\":"
    elif titulo:
        narrativa = f"Fragmento del documento \"{titulo[:80]}\":"
    else:
        narrativa = "Fragmento de documento de la Gaceta UMSA:"

    return f"{cabecera}\n{narrativa}\n"

def dividir_chunks(texto: str, cabecera: str, doc_id: str) -> list[dict]:
    """Divide un texto en chunks de ~CHUNK_SIZE chars con overlap."""
    chunks = []
    inicio, idx = 0, 0
    n = len(texto)

    while inicio < n and idx < MAX_CHUNKS_DOC:
        fin   = min(inicio + CHUNK_SIZE, n)
        trozo = texto[inicio:fin]

        if fin < n:
            corte = trozo.rfind("\n")
            if corte > CHUNK_SIZE // 2:
                trozo = trozo[:corte]
                fin   = inicio + corte
            else:
                corte_p = trozo.rfind(". ")
                if corte_p > CHUNK_SIZE // 2:
                    trozo = trozo[:corte_p + 1]
                    fin   = inicio + corte_p + 1

        trozo = trozo.strip()
        if len(trozo) >= CHUNK_MIN_LEN:
            chunks.append({
                "id":          f"{doc_id}_v2_c{idx:04d}",
                "texto":       cabecera + trozo,
                "texto_puro":  trozo,
                "doc_id":      doc_id,
                "chunk_index": idx,
            })
            idx += 1

        nuevo_inicio = fin - CHUNK_OVERLAP
        if nuevo_inicio <= inicio:
            nuevo_inicio = inicio + max(1, CHUNK_SIZE // 2)
        inicio = nuevo_inicio

    return chunks

def info_estado_bd() -> dict:
    """
    Inspecciona la base actual y devuelve un resumen.

    Devuelve dict con:
      - sqlite_existe: bool
      - faiss_existe:  bool
      - n_chunks:      int (0 si no existe)
      - n_docs:        int (0 si no existe)
      - n_raw:         int (cantidad de doc_*.json en data/raw/)
      - tam_sqlite_mb: float
      - tam_faiss_mb:  float
      - fecha_faiss:   str (fecha de modificacion del FAISS, o '')
    """
    from datetime import datetime as _dt

    info = {
        "sqlite_existe": RUTA_SQLITE.exists(),
        "faiss_existe":  RUTA_FAISS.exists(),
        "n_chunks":      0,
        "n_docs":        0,
        "n_raw":         0,
        "tam_sqlite_mb": 0.0,
        "tam_faiss_mb":  0.0,
        "fecha_faiss":   "",
    }

    if CARPETA_RAW.exists():
        info["n_raw"] = sum(1 for _ in CARPETA_RAW.glob("doc_*.json"))

    if info["sqlite_existe"]:
        try:
            info["tam_sqlite_mb"] = RUTA_SQLITE.stat().st_size / 1024 / 1024
            conn = sqlite3.connect(str(RUTA_SQLITE))
            try:
                row = conn.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT doc_id) FROM chunks_meta"
                ).fetchone()
                info["n_chunks"], info["n_docs"] = row
            except sqlite3.OperationalError:
                pass
            finally:
                conn.close()
        except Exception:
            pass

    if info["faiss_existe"]:
        try:
            st = RUTA_FAISS.stat()
            info["tam_faiss_mb"] = st.st_size / 1024 / 1024
            info["fecha_faiss"]  = _dt.fromtimestamp(st.st_mtime)\
                                     .strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass

    return info

def imprimir_estado_bd(info: dict) -> None:
    """Imprime un bloque legible con el estado actual de la base."""
    log("")
    log("  Estado actual de la base de datos:")
    log("  " + "-" * 60)
    if info["sqlite_existe"]:
        log(f"    chunks_v2.sqlite     : {info['tam_sqlite_mb']:.1f} MB")
        log(f"    chunks indexados     : {info['n_chunks']:,}")
        log(f"    documentos unicos    : {info['n_docs']:,}")
    else:
        log(f"    chunks_v2.sqlite     : (no existe — base vacia)")
    if info["faiss_existe"]:
        log(f"    faiss_index_v2.bin   : {info['tam_faiss_mb']:.1f} MB"
            f"  (actualizado: {info['fecha_faiss']})")
    else:
        log(f"    faiss_index_v2.bin   : (no existe)")
    log(f"    archivos en data/raw/: {info['n_raw']:,} doc_*.json")
    nuevos = max(0, info["n_raw"] - info["n_docs"])
    if info["sqlite_existe"] and info["n_docs"] > 0:
        log(f"    docs potencialmente nuevos en raw/: ~{nuevos:,}")
    log("  " + "-" * 60)

def preguntar_modo_interactivo() -> dict | None:
    """
    Menu interactivo que se muestra al inicio si no se paso ninguna flag
    de modo en la linea de comandos. Devuelve un dict con las flags
    equivalentes (reset, reanudar) o None si el usuario cancela.

    Opciones:
      1) SOBRESCRIBIR TODA LA BASE  -> equivale a --reset
      2) SOLO DOCUMENTOS NUEVOS O CAMBIADOS -> equivale a --reanudar
      3) Cancelar y salir
    """
    info = info_estado_bd()
    imprimir_estado_bd(info)

    log("")
    log("  ¿Que quieres hacer?")
    log("  " + "=" * 60)
    log("    1) SOBRESCRIBIR TODA LA BASE DE DATOS")
    log("       - Borra chunks_v2.sqlite y reconstruye desde cero.")
    log("       - Re-procesa TODOS los doc_*.json en data/raw/.")
    log("       - Re-embebe TODOS los chunks (lento: varios minutos).")
    log("       - Util cuando: cambio el OCR, cambio la logica de chunking,")
    log("         o quieres purgar chunks huerfanos.")
    log("")
    log("    2) SOLO DOCUMENTOS NUEVOS O CAMBIADOS")
    log("       - Conserva los chunks de los documentos que no cambiaron.")
    log("       - Procesa los doc_*.json NUEVOS y los que CAMBIARON")
    log("         (se compara la huella del OCR, no solo el doc_id).")
    log("       - FAISS se reconstruye al final (con nuevos + viejos).")
    log("       - Util cuando: anadiste docs nuevos a data/raw/, o")
    log("         reemplazaste el OCR de alguno que ya estaba.")
    log("")
    log("    3) Cancelar y salir")
    log("  " + "=" * 60)

    if not info["sqlite_existe"]:
        log("")
        log("  [aviso] No existe chunks_v2.sqlite todavia.")
        log("  La opcion 2 no aplica; usa la 1 para crear la base por primera vez.")

    while True:
        try:
            eleccion = input("\n  Tu eleccion [1/2/3]: ").strip()
        except (KeyboardInterrupt, EOFError):
            log("\n  Cancelado.")
            return None

        if eleccion == "1":
            if info["sqlite_existe"] and info["n_chunks"] > 0:
                try:
                    confirm = input(
                        f"\n  Esto BORRARA {info['n_chunks']:,} chunks de "
                        f"{info['n_docs']:,} documentos.\n"
                        f"  ¿Confirmar sobrescritura completa? [s/N]: "
                    ).strip().lower()
                except (KeyboardInterrupt, EOFError):
                    log("\n  Cancelado.")
                    return None
                if confirm not in ("s", "si", "sí", "y", "yes"):
                    log("  Sobrescritura cancelada. Elige otra opcion.")
                    continue
            return {"reset": True, "reanudar": False}

        if eleccion == "2":
            if not info["sqlite_existe"]:
                log("  No hay base previa. No se puede 'solo buscar nuevos'. "
                    "Elige 1 para crearla.")
                continue
            return {"reset": False, "reanudar": True}

        if eleccion in ("3", "q", "salir", "exit"):
            log("  Cancelado por el usuario.")
            return None

        log("  Opcion invalida. Escribe 1, 2 o 3.")

def crear_o_abrir_sqlite(reset: bool = False) -> sqlite3.Connection:
    """Crea/abre la BD SQLite v2."""
    if reset and RUTA_SQLITE.exists():
        RUTA_SQLITE.unlink()

    fresh = not RUTA_SQLITE.exists()
    conn = sqlite3.connect(str(RUTA_SQLITE))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")

    if fresh:
        conn.execute("""
            CREATE VIRTUAL TABLE chunks USING fts5(
                chunk_id, doc_id, texto, tokenize='unicode61'
            )
        """)
        conn.execute("""
            CREATE TABLE chunks_meta (
                chunk_id    TEXT PRIMARY KEY,
                doc_id      TEXT,
                chunk_index INTEGER,
                texto_emb   TEXT
            )
        """)
        conn.execute("CREATE INDEX idx_doc ON chunks_meta(doc_id)")
        conn.commit()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS docs_meta (
            doc_id      TEXT PRIMARY KEY,
            hash        TEXT,
            n_chunks    INTEGER,
            actualizado TEXT
        )
    """)
    conn.commit()
    return conn

def docs_ya_procesados(conn: sqlite3.Connection) -> set:
    """Devuelve set de doc_ids que ya tienen chunks en la BD (para reanudar)."""
    rows = conn.execute("SELECT DISTINCT doc_id FROM chunks_meta").fetchall()
    return {r[0] for r in rows}

def hash_contenido(contenido: str) -> str:
    """Huella del OCR CRUDO del documento (antes de limpiar y chunkear): si
    cambia, los chunks guardados ya no representan al documento.
    Mismo criterio que `extraer_grafo_llm.doc_hash` (sha1 truncado a 12), para
    que la base vectorial y el grafo consideren 'cambiado' a los MISMOS docs."""
    return hashlib.sha1((contenido or "").encode("utf-8", "ignore")).hexdigest()[:12]

def hashes_indexados(conn: sqlite3.Connection) -> dict:
    """doc_id -> huella del contenido con el que se indexo (tabla docs_meta)."""
    try:
        rows = conn.execute("SELECT doc_id, hash FROM docs_meta").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r[0]: r[1] for r in rows}

def decidir_doc(hash_indexado: str | None, tiene_chunks: bool,
                hash_nuevo: str) -> str:
    """Que hacer con un documento en modo incremental. Funcion PURA (testeable).

      'nuevo'       no esta en la base           -> chunkear
      'sin_cambios' misma huella                 -> saltar
      'cambiado'    otra huella                  -> re-chunkear (borrando antes)
      'backfill'    tiene chunks pero no huella  -> registrar la huella y saltar

    'backfill' es el caso de una base construida antes de que existiera
    docs_meta: se asume que sus chunks corresponden al raw actual (exactamente
    lo que asumia el viejo salto por doc_id) y solo se anota la huella, para
    que a partir de la proxima corrida el doc entre por el camino normal.
    """
    if hash_indexado is not None:
        return "sin_cambios" if hash_indexado == hash_nuevo else "cambiado"
    return "backfill" if tiene_chunks else "nuevo"

def borrar_chunks_de(cur: sqlite3.Cursor, doc_id: str) -> None:
    """Borra los chunks de un documento en las DOS tablas. Imprescindible antes
    de re-chunkear: en la tabla FTS5 `chunks` un INSERT OR REPLACE no reemplaza
    nada (fts5 no tiene clave unica) y dejaria filas DUPLICADAS; y si el texto
    nuevo genera menos chunks, los sobrantes quedarian huerfanos."""
    cur.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
    cur.execute("DELETE FROM chunks_meta WHERE doc_id = ?", (doc_id,))

def registrar_doc(cur: sqlite3.Cursor, doc_id: str, huella: str,
                  n_chunks: int | None) -> None:
    """Anota la huella con la que quedo indexado el documento."""
    cur.execute(
        "INSERT OR REPLACE INTO docs_meta VALUES (?,?,?,?)",
        (doc_id, huella, n_chunks, datetime.now().isoformat(timespec="seconds")),
    )

def contar_chunks_en_base() -> int:
    """Chunks que hay EN LA BASE (no los generados en la ultima corrida).
    La Fase B se decide con esto: en un incremental donde no cambio nada la
    corrida genera 0 chunks y la base esta completa — saltarse la Fase B ahi
    dejaria los embeddings desactualizados sin avisar."""
    if not RUTA_SQLITE.exists():
        return 0
    conn = sqlite3.connect(str(RUTA_SQLITE))
    try:
        return conn.execute("SELECT COUNT(*) FROM chunks_meta").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()

def fase_a_chunking(max_ocr_bytes: int, limit: int | None,
                    reset: bool, reanudar: bool):
    """
    Lee data/raw/*.json uno a uno, genera chunks, los guarda en SQLite.
    Imprime UN MENSAJE POR DOCUMENTO para que sepamos exactamente donde estamos.
    """
    archivos = sorted(CARPETA_RAW.glob("doc_*.json"))
    if not archivos:
        log(f"  ERROR: no hay archivos en {CARPETA_RAW}")
        log("  Ejecuta primero: python gaceta_rag/main.py")
        sys.exit(1)

    if limit:
        archivos = archivos[:limit]
        log(f"  [LIMIT] solo procesara los primeros {limit} archivos")

    log(f"  Total archivos en raw   : {len(archivos)}")
    log(f"  Limite OCR por doc      : {max_ocr_bytes/1024/1024:.1f} MB")
    log(f"  Modo                    : {'reanudar' if reanudar else ('reset' if reset else 'crear nuevo')}")

    conn = crear_o_abrir_sqlite(reset=reset)
    cur  = conn.cursor()

    hashes_prev     = hashes_indexados(conn)
    docs_con_chunks = docs_ya_procesados(conn)
    if reanudar and docs_con_chunks:
        log(f"  [REANUDAR] {len(docs_con_chunks)} docs ya indexados; se saltan "
            f"los que no hayan cambiado (huella del OCR)")
        sin_huella = len(docs_con_chunks) - len(hashes_prev)
        if sin_huella > 0:
            log(f"  [REANUDAR] {sin_huella} de ellos son de una base anterior a "
                f"docs_meta; se les registrara la huella sin re-chunkear")

    log_prog = open(RUTA_LOG_PROG, "a", encoding="utf-8")
    log_skip = open(RUTA_LOG_SKIP, "a", encoding="utf-8")
    log_prog.write(f"\n# {datetime.now().isoformat()} Fase A inicio\n")
    log_skip.write(f"\n# {datetime.now().isoformat()} Fase A inicio\n")

    n_ok       = 0
    n_skip     = 0
    n_chunks   = 0
    n_pending  = 0
    n_nuevos   = 0
    n_cambiados = 0
    n_igual    = 0
    t0         = time.time()

    log("")
    log("  Procesando documentos uno por uno (impresion por cada uno):")
    log("  " + "-" * 66)

    for i, archivo in enumerate(archivos, 1):
        nombre = archivo.name
        prefix = f"  [{i:5d}/{len(archivos)}] {nombre}"

        try:
            sz_bytes = archivo.stat().st_size
        except Exception as e:
            log(f"{prefix} -> ERROR stat: {e}")
            log_skip.write(f"{nombre}\tSTAT_ERROR={e}\n")
            n_skip += 1
            continue

        if sz_bytes > max_ocr_bytes * 3:
            log(f"{prefix} -> SKIP (archivo {sz_bytes/1024/1024:.1f} MB)")
            log_skip.write(f"{nombre}\tFILE_TOO_BIG={sz_bytes}\n")
            n_skip += 1
            continue

        try:
            with open(archivo, encoding="utf-8") as f:
                doc = json.load(f)
        except Exception as e:
            log(f"{prefix} -> ERROR JSON: {e}")
            log_skip.write(f"{nombre}\tJSON_ERROR={e}\n")
            n_skip += 1
            continue

        pid = doc.get("paperlessId") or doc.get("id") or 0
        doc_id = f"doc_{int(pid):05d}"

        contenido = doc.get("contenido") or ""

        h_nuevo = hash_contenido(contenido)
        accion  = decidir_doc(hashes_prev.get(doc_id),
                              doc_id in docs_con_chunks, h_nuevo)

        if reanudar and accion == "sin_cambios":
            log(f"{prefix} -> sin cambios, saltando")
            n_igual += 1
            del doc, contenido
            continue

        if reanudar and accion == "backfill":
            registrar_doc(cur, doc_id, h_nuevo, None)
            n_pending += 1
            n_igual   += 1
            log(f"{prefix} -> ya indexado (huella registrada)")
            del doc, contenido
            continue

        if not contenido.strip():
            log(f"{prefix} -> sin OCR")
            log_skip.write(f"{nombre}\tSIN_OCR\n")
            del doc, contenido
            continue

        if len(contenido) > max_ocr_bytes:
            log(f"{prefix} -> SKIP OCR={len(contenido)/1024/1024:.2f}MB > limite")
            log_skip.write(f"{nombre}\tOCR_TOO_BIG={len(contenido)}\n")
            del doc, contenido
            continue

        t_clean_start = time.time()
        try:
            limpio = limpiar_ocr_seguro(contenido)
        except Exception as e:
            log(f"{prefix} -> ERROR limpieza: {e}")
            log_skip.write(f"{nombre}\tCLEAN_ERROR={e}\n")
            n_skip += 1
            del doc, contenido
            continue
        t_clean = time.time() - t_clean_start

        if not limpio or len(limpio) < CHUNK_MIN_LEN:
            log(f"{prefix} -> queda vacio tras limpieza")
            log_skip.write(f"{nombre}\tEMPTY_AFTER_CLEAN\n")
            del doc, contenido, limpio
            continue

        try:
            cabecera = cabecera_enriquecida(doc)
            chunks   = dividir_chunks(limpio, cabecera, doc_id)
        except Exception as e:
            log(f"{prefix} -> ERROR chunking: {e}")
            log_skip.write(f"{nombre}\tCHUNK_ERROR={e}\n")
            n_skip += 1
            del doc, contenido, limpio
            continue

        if not chunks:
            log(f"{prefix} -> sin chunks generados")
            del doc, contenido, limpio
            continue

        try:
            if doc_id in docs_con_chunks:
                borrar_chunks_de(cur, doc_id)
            for ch in chunks:
                cur.execute("INSERT OR REPLACE INTO chunks VALUES (?,?,?)",
                            (ch["id"], ch["doc_id"], ch["texto_puro"]))
                cur.execute("INSERT OR REPLACE INTO chunks_meta VALUES (?,?,?,?)",
                            (ch["id"], ch["doc_id"], ch["chunk_index"], ch["texto"]))
            registrar_doc(cur, doc_id, h_nuevo, len(chunks))
        except Exception as e:
            log(f"{prefix} -> ERROR sqlite: {e}")
            log_skip.write(f"{nombre}\tSQL_ERROR={e}\n")
            del doc, contenido, limpio, chunks
            continue

        n_chunks  += len(chunks)
        n_ok      += 1
        n_pending += 1
        if accion == "cambiado":
            n_cambiados += 1
        elif accion == "nuevo":
            n_nuevos += 1

        etiqueta = " [CAMBIADO]" if accion == "cambiado" else ""
        log(f"{prefix} -> OK{etiqueta} {len(chunks)} chunks "
            f"(OCR {len(contenido)/1024:.1f}KB, limpieza {t_clean*1000:.0f}ms)")
        log_prog.write(f"{nombre}\t{len(chunks)}\t{len(contenido)}\t{t_clean:.3f}\n")

        del doc, contenido, limpio, chunks

        if n_pending >= COMMIT_CADA_DOCS:
            conn.commit()
            n_pending = 0
            gc.collect()
            elapsed = time.time() - t0
            rate = n_ok / elapsed if elapsed > 0 else 0
            log(f"  ... commit ({n_ok} ok, {n_chunks} chunks, {rate:.1f} doc/s)")

    conn.commit()
    conn.close()
    log_prog.close()
    log_skip.close()

    elapsed = time.time() - t0
    log("")
    log("  " + "-" * 66)
    log(f"  FASE A completada en {elapsed:.1f}s")
    log(f"    Documentos OK         : {n_ok}")
    if reanudar:
        log(f"      nuevos              : {n_nuevos}")
        log(f"      cambiados (re-chunk): {n_cambiados}")
        log(f"      sin cambios         : {n_igual}")
    log(f"    Documentos saltados   : {n_skip}")
    log(f"    Chunks generados      : {n_chunks}")
    log(f"    Velocidad             : {n_ok/elapsed if elapsed>0 else 0:.2f} doc/s")
    log(f"    SQLite                : {RUTA_SQLITE.stat().st_size/1024/1024:.1f} MB")
    log(f"    Log progreso          : {RUTA_LOG_PROG}")
    log(f"    Log skipped           : {RUTA_LOG_SKIP}")
    return n_ok, n_chunks

def leer_chunks_actuales(conn: sqlite3.Connection) -> list:
    """[(chunk_id, doc_id, hash_texto)] en orden de rowid.
    Recorre en streaming y NO se queda con los textos, solo con su huella."""
    cur = conn.execute(
        "SELECT chunk_id, doc_id, texto_emb FROM chunks_meta ORDER BY rowid")
    return [(cid, did, hash_contenido(txt)) for cid, did, txt in cur]

def cargar_indice_previo(modelo_id: str, dim_modelo: int):
    """Devuelve ((index, metadata), "") si el indice de la corrida anterior se
    puede reciclar, o (None, motivo) si hay que re-embeber todo. Es MUY
    conservador: ante cualquier duda, rebuild completo."""
    if not (RUTA_FAISS.exists() and RUTA_META.exists()):
        return None, "no hay indice previo"

    if RUTA_CONFIG.exists():
        try:
            with open(RUTA_CONFIG, encoding="utf-8") as f:
                cfg_prev = json.load(f)
        except Exception:
            return None, "embedding_config_v2.json ilegible"
        if cfg_prev.get("modelo_embeddings") != modelo_id:
            return None, (f"el indice previo es de {cfg_prev.get('modelo_embeddings')} "
                          f"y ahora se pide {modelo_id}")
    else:
        return None, "no hay embedding_config_v2.json (no se sabe con que modelo se construyo)"

    import faiss
    try:
        index = faiss.read_index(str(RUTA_FAISS))
        with open(RUTA_META, encoding="utf-8") as f:
            metadata = json.load(f)
    except Exception as e:
        return None, f"no se pudo leer el indice previo ({e})"

    if index.d != dim_modelo:
        return None, f"el indice previo tiene {index.d} dims y el modelo {dim_modelo}"
    if index.ntotal != len(metadata):
        return None, (f"indice ({index.ntotal}) y metadata ({len(metadata)}) "
                      f"desincronizados")
    if not all(isinstance(m, dict) and "hash_texto" in m for m in metadata):
        return None, ("la metadata previa no trae hash_texto (se construyo antes "
                      "del embedding incremental); esta corrida la deja lista")
    return (index, metadata), ""

def plan_embeddings(metadata_prev: list, chunks_actuales: list):
    """Funcion PURA: decide que vectores se reciclan y cuales hay que calcular.

    Un vector se recicla SOLO si su chunk_id sigue en la base Y el texto que se
    embebio entonces es IDENTICO al de ahora (misma huella). El chunk_id por si
    solo no sirve como llave: al re-chunkear un documento que cambio, los ids
    (doc_00001_v2_c0000...) se repiten pero el texto es otro — reciclar por id
    dejaria vectores viejos apuntando a texto nuevo.

    Devuelve (reusar, nuevos, solo_anexa):
      reusar  [(pos_prev, chunk_id, doc_id, hash_texto)] en el orden previo
      nuevos  [(chunk_id, doc_id, hash_texto)] a embeber, en orden de rowid
      solo_anexa  True si no se cayo ningun vector: basta con anadir al final
    """
    actual = {cid: (did, h) for cid, did, h in chunks_actuales}
    reusar, vistos = [], set()
    for pos, m in enumerate(metadata_prev):
        cid = m.get("chunk_id")
        par = actual.get(cid)
        if par is None or cid in vistos:
            continue
        did, h = par
        if m.get("hash_texto") != h:
            continue
        reusar.append((pos, cid, did, h))
        vistos.add(cid)
    nuevos = [(cid, did, h) for cid, did, h in chunks_actuales if cid not in vistos]
    return reusar, nuevos, len(reusar) == len(metadata_prev)

def embeber_pendientes(conn, index, metadata: list, pendientes: dict,
                       modelo, usa_e5: bool, batch_size: int) -> int:
    """Recorre chunks_meta en streaming y embebe SOLO los chunk_id de
    `pendientes` (chunk_id -> (doc_id, hash_texto)), anadiendolos al indice y a
    la metadata. La posicion en la metadata SIEMPRE coincide con la del vector
    en FAISS (los consumidores hacen faiss_meta[idx])."""
    import numpy as np

    n_total = len(pendientes)
    if n_total == 0:
        return 0

    buffer = []
    hechos = 0
    t0 = last_print = time.time()

    def encode_y_anadir(items):
        nonlocal hechos
        if not items:
            return
        textos = [it[2] for it in items]
        if usa_e5:
            textos = ["passage: " + t for t in textos]
        vecs = modelo.encode(
            textos,
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        ).astype(np.float32)
        index.add(vecs)
        for cid, did, _ in items:
            metadata.append({
                "pos":        len(metadata),
                "chunk_id":   cid,
                "doc_id":     did,
                "hash_texto": pendientes[cid][1],
            })
        hechos += len(items)
        del vecs

    cur = conn.execute(
        "SELECT chunk_id, doc_id, texto_emb FROM chunks_meta ORDER BY rowid")
    for chunk_id, doc_id, texto_emb in cur:
        if chunk_id not in pendientes:
            continue
        buffer.append((chunk_id, doc_id, texto_emb))
        if len(buffer) < batch_size:
            continue

        try:
            encode_y_anadir(buffer)
        except Exception as e:
            log(f"  [ERROR encode batch en {hechos}: {e}]")
            for it in buffer:
                try:
                    encode_y_anadir([it])
                except Exception:
                    pass
        buffer.clear()

        ahora = time.time()
        if (ahora - last_print) > 5.0:
            pct  = hechos / n_total * 100
            rate = hechos / (ahora - t0) if ahora > t0 else 0
            eta  = (n_total - hechos) / rate if rate > 0 else 0
            log(f"  [{pct:5.1f}%] {hechos}/{n_total} chunks "
                f"({rate:.0f} ch/s, ETA {eta:.0f}s)")
            last_print = ahora
            gc.collect()

    if buffer:
        try:
            encode_y_anadir(buffer)
        except Exception as e:
            log(f"  [ERROR ultimo batch: {e}]")

    return hechos

def fase_b_embeddings(modelo_id: str, batch_size: int, incremental: bool = False):
    """
    Embebe los chunks de SQLite y escribe FAISS + metadata.

    Con `incremental=True` recicla del indice anterior los vectores cuyo texto
    no cambio y solo calcula los que faltan (ver plan_embeddings). Si el indice
    previo no se puede reciclar con seguridad (otro modelo, otra dimension,
    metadata sin huellas, archivos desincronizados) cae solo al rebuild
    completo, avisando por que.

    Memoria pico acotada: el modelo + un batch a la vez. Al rearmar el indice
    se reconstruyen los vectores conservados (4 bytes x dim x chunks).
    """
    import numpy as np
    from sentence_transformers import SentenceTransformer
    import faiss

    log(f"\n  Cargando modelo: {modelo_id}")
    log(f"  (primera vez? puede descargar 0.5-2.3 GB)")
    t_load = time.time()
    modelo = SentenceTransformer(modelo_id)
    dim    = modelo.get_sentence_embedding_dimension()
    usa_e5 = "e5" in modelo_id.lower()
    log(f"  Modelo cargado en {time.time()-t_load:.1f}s")
    log(f"  Dimensiones: {dim}")
    if usa_e5:
        log(f"  Usando prefijo 'passage:' (modelo E5)")

    conn = sqlite3.connect(str(RUTA_SQLITE))
    chunks_actuales = leer_chunks_actuales(conn)
    n_total = len(chunks_actuales)
    log(f"  Chunks en la base: {n_total}")
    if n_total == 0:
        log("  No hay chunks. Saliendo de Fase B.")
        conn.close()
        return dim, 0

    index      = None
    metadata   = []
    pendientes = {}
    reciclados = 0
    t0 = time.time()

    if incremental:
        prev, motivo = cargar_indice_previo(modelo_id, dim)
        if prev is None:
            log(f"  [INCREMENTAL] no se puede reciclar el indice previo: {motivo}")
            log(f"  [INCREMENTAL] -> se embebe todo (la proxima corrida ya sera incremental)")
        else:
            index_prev, metadata_prev = prev
            reusar, nuevos, solo_anexa = plan_embeddings(metadata_prev, chunks_actuales)
            reciclados = len(reusar)
            caidos     = len(metadata_prev) - reciclados
            log(f"  [INCREMENTAL] vectores reciclados : {reciclados}")
            log(f"  [INCREMENTAL] vectores descartados: {caidos} "
                f"(chunks borrados o con texto distinto)")
            log(f"  [INCREMENTAL] a calcular          : {len(nuevos)}")

            if solo_anexa:
                index    = index_prev
                metadata = [dict(m) for m in metadata_prev]
            else:
                log(f"  [INCREMENTAL] rearmando el indice con los vectores conservados...")
                vecs_prev = index_prev.reconstruct_n(0, index_prev.ntotal)
                index = faiss.IndexFlatIP(dim)
                for i in range(0, reciclados, 4096):
                    lote = reusar[i:i + 4096]
                    index.add(np.ascontiguousarray(
                        vecs_prev[[p for p, _, _, _ in lote]]))
                    for _, cid, did, h in lote:
                        metadata.append({"pos": len(metadata), "chunk_id": cid,
                                         "doc_id": did, "hash_texto": h})
                del vecs_prev, index_prev
                gc.collect()

            pendientes = {cid: (did, h) for cid, did, h in nuevos}

    if index is None:
        index      = faiss.IndexFlatIP(dim)
        metadata   = []
        reciclados = 0
        pendientes = {cid: (did, h) for cid, did, h in chunks_actuales}

    log(f"\n  Embedding por batches ({len(pendientes)} chunks):")
    n_embebidos = embeber_pendientes(conn, index, metadata, pendientes,
                                     modelo, usa_e5, batch_size)
    conn.close()

    if index.ntotal != len(metadata):
        log(f"  [ERROR] indice ({index.ntotal}) y metadata ({len(metadata)}) "
            f"no coinciden; no se guarda nada")
        return dim, 0

    elapsed = time.time() - t0
    log(f"\n  FASE B completada en {elapsed:.1f}s")
    log(f"    Vectores en FAISS    : {index.ntotal}")
    if reciclados:
        log(f"      reciclados         : {reciclados}")
        log(f"      calculados ahora   : {n_embebidos}")
    log(f"    Velocidad            : {n_embebidos/elapsed if elapsed>0 else 0:.0f} ch/s")

    log(f"\n  Guardando indice FAISS...")
    faiss.write_index(index, str(RUTA_FAISS))
    log(f"    {RUTA_FAISS.name}: {RUTA_FAISS.stat().st_size/1024/1024:.1f} MB")

    log(f"  Guardando metadata...")
    with open(RUTA_META, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False)
    log(f"    {RUTA_META.name}: {RUTA_META.stat().st_size/1024/1024:.1f} MB")

    return dim, index.ntotal

def guardar_config(modelo_id: str, dim: int, n_chunks: int, n_docs: int):
    config = {
        "modelo_embeddings":   modelo_id,
        "dimensiones":         dim,
        "n_chunks":            n_chunks,
        "n_documentos":        n_docs,
        "chunk_size":          CHUNK_SIZE,
        "chunk_overlap":       CHUNK_OVERLAP,
        "fecha_construccion":  datetime.now().isoformat(timespec="seconds"),
        "requiere_prefijo_e5": "e5" in modelo_id.lower(),
        "version":             "v2",
        "generado_por":        "main3.py",
    }
    with open(RUTA_CONFIG, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    log(f"  Config: {RUTA_CONFIG.name}")

def main():
    p = argparse.ArgumentParser(
        description="Pipeline v3 streaming",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--modelo", default=MODELO_DEFAULT, choices=list(MODELOS.keys()))
    p.add_argument("--solo-chunks", action="store_true",
                   help="Solo Fase A (chunking). No embeddings.")
    p.add_argument("--solo-embed", action="store_true",
                   help="Solo Fase B (embed). Usa chunks_v2.sqlite existente.")
    p.add_argument("--max-ocr-mb", type=float, default=MAX_OCR_MB_DEFAULT,
                   help=f"Skip docs con OCR > N MB (default {MAX_OCR_MB_DEFAULT})")
    p.add_argument("--limit", type=int, default=None,
                   help="Procesar solo N archivos (test)")
    p.add_argument("--batch-size", type=int, default=EMBED_BATCH_SIZE)
    p.add_argument("--reset", action="store_true",
                   help="Borrar chunks_v2.sqlite y empezar de cero")
    p.add_argument("--reanudar", action="store_true",
                   help="Incremental: procesar solo los docs nuevos o cuyo "
                        "contenido cambio (huella sha1 del OCR en docs_meta)")
    p.add_argument("--reembeber-todo", action="store_true",
                   help="Fase B completa: recalcula TODOS los embeddings en vez "
                        "de reciclar los del indice anterior")
    p.add_argument("--no-menu", action="store_true",
                   help="Desactivar el menu interactivo (modo por defecto: "
                        "INSERT OR REPLACE sobre la BD existente)")
    args = p.parse_args()

    flags_modo_explicitas = (args.reset or args.reanudar
                             or args.solo_embed or args.solo_chunks
                             or args.no_menu)
    if not flags_modo_explicitas:
        modo = preguntar_modo_interactivo()
        if modo is None:
            log("  Saliendo sin cambios.")
            return
        args.reset    = modo["reset"]
        args.reanudar = modo["reanudar"]

    modelo_id, tam, dim_esp = MODELOS[args.modelo]

    banner("MAIN3 — Pipeline streaming v3")
    log(f"  Carpeta raw       : {CARPETA_RAW}")
    log(f"  Modelo embeddings : {modelo_id}")
    log(f"  Tamano descarga   : {tam}")
    log(f"  Dimensiones esp.  : {dim_esp}")
    log(f"  Chunk size/over   : {CHUNK_SIZE}/{CHUNK_OVERLAP}")
    log(f"  Max OCR por doc   : {args.max_ocr_mb} MB")
    log(f"  Batch embeddings  : {args.batch_size}")
    if args.limit:
        log(f"  LIMIT             : {args.limit} docs")
    log(f"  Modo              : "
        f"{'solo-chunks' if args.solo_chunks else 'solo-embed' if args.solo_embed else 'completo'}")
    log(f"  Sistema v1 actual : INTACTO")

    n_ok = n_chunks = 0

    if not args.solo_embed:
        banner("FASE A — Chunks de raw -> SQLite", char="-")
        n_ok, _n_chunks_corrida = fase_a_chunking(
            max_ocr_bytes=int(args.max_ocr_mb * 1024 * 1024),
            limit=args.limit,
            reset=args.reset,
            reanudar=args.reanudar,
        )
        n_chunks = contar_chunks_en_base()
        if n_chunks == 0 and not args.solo_chunks:
            log("\n  No hay chunks en la base. Abortando Fase B.")
            return
    else:
        if not RUTA_SQLITE.exists():
            log(f"  ERROR: --solo-embed pero no existe {RUTA_SQLITE}")
            return
        conn = sqlite3.connect(str(RUTA_SQLITE))
        n_chunks = conn.execute("SELECT COUNT(*) FROM chunks_meta").fetchone()[0]
        n_ok     = conn.execute("SELECT COUNT(DISTINCT doc_id) FROM chunks_meta").fetchone()[0]
        conn.close()
        log(f"\n  [solo-embed] usando {n_chunks} chunks de {n_ok} docs ya en SQLite")

    dim_real = 0
    if not args.solo_chunks:
        incremental_embed = not (args.reset or args.reembeber_todo)
        banner("FASE B — Embeddings -> FAISS"
               + (" (incremental)" if incremental_embed else ""), char="-")
        dim_real, n_vec = fase_b_embeddings(modelo_id, batch_size=args.batch_size,
                                            incremental=incremental_embed)
        conn = sqlite3.connect(str(RUTA_SQLITE))
        n_docs_tot = conn.execute(
            "SELECT COUNT(DISTINCT doc_id) FROM chunks_meta").fetchone()[0]
        conn.close()
        guardar_config(modelo_id, dim_real, n_vec, n_docs_tot)

    banner("COMPLETADO")
    log(f"  Documentos OK : {n_ok}")
    log(f"  Chunks        : {n_chunks}")
    log(f"  Modelo        : {modelo_id}")
    log(f"  Dimensiones   : {dim_real or '(no se hizo Fase B)'}")
    log("")
    log("  Para usar la nueva base desde el chat:")
    log("    ejecutar_qwen25_7b_v3.bat")

if __name__ == "__main__":
    main()
