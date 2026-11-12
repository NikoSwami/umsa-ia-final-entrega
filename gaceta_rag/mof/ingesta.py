# -*- coding: utf-8 -*-
"""
Ingesta EN CALIENTE de una unidad de MOF: un evento -> un documento
reindexado, sin reconstruir la base ni reiniciar el motor.

Para que existe
───────────────
El pipeline normal (construir_todo.py) reconstruye la base entera y el motor
lee el indice UNA vez al arrancar (motor_web.py:723). Eso sirve para el corpus
de resoluciones, que cambia por lotes. Para MOF no alcanza: cuando alguien
edita una unidad, esa edicion tiene que verse en el chat en segundos.

Que hace y que NO hace
──────────────────────
Reindexa UN documento: reescribe su ficha en data/raw/, rehace sus chunks en
SQLite y añade sus vectores al indice FAISS que el motor ya tiene cargado en
memoria. No toca ningun otro documento.

Tres restricciones REALES del codigo actual, y como se resuelven aqui:

  1. La conexion del motor es de SOLO LECTURA
     (motor_web.py:731, `PRAGMA query_only = ON`). Por eso la ingesta abre su
     PROPIA conexion de escritura y pone la base en modo WAL, para que escribir
     no bloquee las consultas que el motor esta atendiendo.

  2. El indice es un `faiss.IndexFlatIP` (generar_base.py:1028): plano, sin
     mapa de ids. Se le pueden AÑADIR vectores, pero borrarlos renumeraria las
     filas y desincronizaria faiss_metadata_v2.json, que es una lista paralela
     indexada por posicion. Por eso el borrado se hace con LAPIDAS: la entrada
     de metadata se marca `borrado: True` y la busqueda densa la salta
     (consultar_gaceta.buscar_faiss_v2). El vector muerto se queda ocupando
     sitio hasta la siguiente reconstruccion completa, que lo compacta.

  3. Persistir el indice cuesta. Escribir faiss_index_v2.bin son ~80 MB: no se
     puede hacer en cada evento si la promesa es "casi en simultaneo". La
     ingesta aplica el cambio EN MEMORIA (ya visible para el motor) y marca la
     base como sucia; `persistir()` la baja a disco cuando conviene (cada N
     eventos, por tiempo, o al apagar). Si el proceso muere antes, lo perdido
     se recupera con la reconciliacion, no con el indice.

DOS DESTINOS, UN SOLO EVENTO
Cada unidad se guarda en dos formas, y las dos se escriben aqui:

  - la FICHA de texto (raw + chunks + vectores), que es lo que permite
    preguntar en lenguaje natural y encontrar por significado;
  - la REPLICA estructurada (mof.sqlite: unidad, unidad_funcion, cargo_unidad),
    que es lo que permite CONTAR y LISTAR exacto — "cuantas unidades dependen
    de tal", "damelas todas" — que una busqueda por parecido no puede hacer.

Se escriben en el mismo `aplicar()` a proposito. Si la replica se llenara por
su cuenta habria dos caminos que pueden desincronizarse; asi, o el evento
actualiza las dos vistas o no actualiza ninguna.

Idempotencia: cada evento trae un `evento_id`. Los ya aplicados se ignoran,
que es lo que hace seguro que MOF reintente cuando no recibe respuesta.
"""
import json
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path

from . import exportador, replica

_AQUI = Path(__file__).resolve()
DATA = _AQUI.parents[1] / "data"
RAW = DATA / "raw"
RUTA_SQLITE = DATA / "chunks_v2.sqlite"
RUTA_FAISS = DATA / "faiss_index_v2.bin"
RUTA_META = DATA / "faiss_metadata_v2.json"

FLUSH_CADA = int(os.environ.get("MOF_FLUSH_CADA") or "25")
FLUSH_SEGUNDOS = float(os.environ.get("MOF_FLUSH_SEGUNDOS") or "300")
MEMORIA_EVENTOS = 512

class ResultadoIngesta(dict):
    """dict con el resumen de lo aplicado (para devolverlo tal cual por HTTP)."""

class Ingestor:
    """Aplica eventos de MOF sobre la base viva.

    Se construye con los objetos que el motor YA tiene cargados; no abre un
    segundo modelo de embeddings ni un segundo indice. Si el motor todavia no
    termino de cargar, `disponible` es False y todo devuelve un no-op: la API
    responde 503 y MOF reintenta.
    """

    def __init__(self, motor=None, raw=None, ruta_sqlite=None,
                 ruta_faiss=None, ruta_meta=None, ruta_replica=None):
        self.motor = motor
        self.ruta_sqlite = Path(ruta_sqlite) if ruta_sqlite else RUTA_SQLITE
        junto = self.ruta_sqlite.parent
        self.raw = Path(raw) if raw else junto / RAW.name
        self.ruta_faiss = (Path(ruta_faiss) if ruta_faiss
                           else junto / RUTA_FAISS.name)
        self.ruta_meta = Path(ruta_meta) if ruta_meta else junto / RUTA_META.name
        self.ruta_replica = Path(
            ruta_replica or os.environ.get("MOF_REPLICA")
            or (self.ruta_sqlite.parent / "mof.sqlite"))
        self._lock = threading.RLock()
        self._eventos = OrderedDict()
        self._sucio = 0
        self._ultimo_flush = time.time()
        self._conn = None
        self._replica = None

    @property
    def disponible(self) -> bool:
        m = self.motor
        return bool(
            m is not None
            and getattr(m, "faiss_index", None) is not None
            and getattr(m, "faiss_meta", None) is not None
            and getattr(m, "modelo_emb", None) is not None
        )

    def estado(self) -> dict:
        return {
            "disponible": self.disponible,
            "pendientes_de_persistir": self._sucio,
            "eventos_recordados": len(self._eventos),
            "vectores": int(getattr(getattr(self.motor, "faiss_index", None),
                                    "ntotal", 0) or 0),
            "replica": self.estado_replica(),
        }

    def estado_replica(self) -> dict:
        """Cuantas unidades tiene la replica estructurada. Si no se puede
        abrir se dice por que, en vez de fingir que esta vacia."""
        try:
            with self._lock:
                return dict(replica.resumen(self.replica()), ok=True)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def conexion(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(str(self.ruta_sqlite), check_same_thread=False)
            try:
                conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.DatabaseError:
                pass
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            self._conn = conn
        return self._conn

    def replica(self):
        """Conexion a la replica estructurada, abierta al primer uso."""
        if self._replica is None:
            self._replica = replica.abrir(self.ruta_replica)
        return self._replica

    def ids_en_replica(self):
        """unidad_ids que hoy estan en la replica, o None si no se pudo leer.

        None y no un conjunto vacio, por la misma razon que en
        docs_indexados(): un vacio falso haria que la reconciliacion se pusiera
        a reindexar el organigrama entero por un error de lectura."""
        with self._lock:
            try:
                return {fila[0] for fila in
                        self.replica().execute("SELECT id FROM unidad")}
            except Exception:
                return None

    def docs_indexados(self):
        """doc_ids que hoy tienen chunks, o None si no se pudo averiguar.

        Se lee bajo el lock a proposito: la conexion se abre con
        check_same_thread=False y la comparte el hilo de la reconciliacion, asi
        que dos consultas simultaneas sobre el mismo objeto sqlite3 se
        pisarian. Todo acceso pasa por aqui o por aplicar().

        Devuelve None —y no un conjunto vacio— cuando la consulta falla: un
        conjunto vacio significa "no hay nada indexado" y haria que la
        reconciliacion reindexara el organigrama entero por un error de
        lectura.

        "Indexado" exige que las DOS mitades digan lo mismo. SQLite confirma
        en cada evento; el indice, solo al persistir. Si el proceso muere entre
        medio quedan desfasados, de dos maneras:

          - la unidad es nueva: SQLite la tiene y el indice no tiene NINGUN
            vector suyo. Invisible para la busqueda densa.
          - la unidad ya estaba y se edito: el indice tiene vectores VIVOS, pero
            de la version anterior del texto. La busqueda la encuentra por lo
            que decia antes y devuelve lo que dice ahora.

        El segundo caso se escapo a la primera version de esta comprobacion,
        que solo miraba si habia vectores; aparecio probando contra MOF real.
        Por eso ahora se compara vector por vector: cada chunk de SQLite tiene
        que tener un vector vivo con su mismo chunk_id y el hash de su texto.

        Solo se comprueban las fichas de MOF: son las unicas que se ingieren en
        caliente. El corpus se construye en frio y en bloque."""
        with self._lock:
            try:
                con_chunks = {fila[0] for fila in self.conexion().execute(
                    "SELECT DISTINCT doc_id FROM chunks_meta")}
                return con_chunks - self._docs_desfasados(con_chunks)
            except Exception:
                return None

    def _docs_desfasados(self, docs) -> set:
        """Fichas de MOF cuyos vectores vivos no son los de su texto actual."""
        meta = getattr(self.motor, "faiss_meta", None)
        mof = {d for d in docs if exportador.es_ficha_mof(d)}
        if meta is None or not mof:
            return set()

        conn = self.conexion()
        if len(mof) == 1:
            filas = conn.execute("SELECT chunk_id, doc_id, texto_emb FROM "
                                 "chunks_meta WHERE doc_id = ?", tuple(mof))
        else:
            filas = conn.execute("SELECT chunk_id, doc_id, texto_emb FROM "
                                 "chunks_meta WHERE doc_id LIKE 'doc_9%'")
        esperado = {}
        for chunk_id, doc_id, texto in filas:
            if doc_id in mof:
                esperado.setdefault(doc_id, set()).add((chunk_id, _hash(texto)))

        vivos = {}
        for m in meta:
            doc_id = m.get("doc_id")
            if doc_id in mof and not m.get("borrado"):
                vivos.setdefault(doc_id, set()).add(
                    (m.get("chunk_id"), m.get("hash_texto")))

        return {d for d in mof if esperado.get(d, set()) != vivos.get(d, set())}

    def leer_meta(self, clave: str):
        with self._lock:
            try:
                fila = self.replica().execute(
                    "SELECT valor FROM replica_meta WHERE clave = ?",
                    (clave,)).fetchone()
                return fila[0] if fila else None
            except Exception:
                return None

    def guardar_meta(self, clave: str, valor) -> None:
        with self._lock:
            conn = self.replica()
            if valor is None:
                conn.execute("DELETE FROM replica_meta WHERE clave = ?",
                             (clave,))
            else:
                conn.execute("INSERT OR REPLACE INTO replica_meta VALUES (?,?)",
                             (clave, str(valor)))
            conn.commit()

    def cerrar(self):
        with self._lock:
            for atributo in ("_conn", "_replica"):
                conn = getattr(self, atributo)
                if conn is None:
                    continue
                try:
                    conn.commit()
                finally:
                    conn.close()
                    setattr(self, atributo, None)

    def aplicar(self, accion: str, unidad: dict = None, funciones=None,
                personal=None, padre: dict = None, unidad_id=None,
                evento_id: str = None, facultad: str = None,
                marcas: dict = None) -> ResultadoIngesta:
        """Aplica UN evento.

        accion : 'upsert' (alta o cambio) | 'delete' (baja logica de la unidad)
        marcas : campos extra para el bloque 'mof' de la ficha. Los usa la
                 reconciliacion para sellar la huella del listado. NO entran en
                 el contenido, asi que sellar no dispara un re-embedding.

        Devuelve el resumen; repetir el mismo evento_id devuelve el resumen
        anterior sin volver a trabajar.
        """
        with self._lock:
            if evento_id and evento_id in self._eventos:
                previo = ResultadoIngesta(self._eventos[evento_id])
                previo["repetido"] = True
                return previo

            if not self.disponible:
                raise MotorNoListo("el motor todavia no termino de cargar")

            accion = (accion or "").strip().lower()
            if accion == "delete":
                res = self._borrar(unidad_id if unidad_id is not None
                                   else (unidad or {}).get("id"))
            elif accion == "upsert":
                res = self._upsert(unidad or {}, funciones, personal, padre,
                                   facultad, marcas)
            else:
                raise ValueError(f"accion desconocida: {accion!r}")

            res["evento_id"] = evento_id
            res["repetido"] = False
            if evento_id:
                self._eventos[evento_id] = dict(res)
                while len(self._eventos) > MEMORIA_EVENTOS:
                    self._eventos.popitem(last=False)

            self._quiza_persistir()
            return res

    def _upsert(self, unidad, funciones, personal, padre, facultad,
                marcas=None):
        ficha = exportador.ficha_de_unidad(unidad, funciones, personal, padre,
                                           facultad)
        if marcas:
            ficha["mof"].update(marcas)
        doc_id = exportador.doc_id_de_unidad(unidad.get("id"))
        h_nuevo = exportador.huella(ficha)

        conn = self.conexion()
        h_previo = _huella_indexada(conn, doc_id)
        if (h_previo == h_nuevo and _tiene_chunks(conn, doc_id)
                and doc_id not in self._docs_desfasados({doc_id})):
            self._escribir_raw(doc_id, ficha)
            res = ResultadoIngesta(accion="upsert", doc_id=doc_id,
                                   sin_cambios=True, chunks=0, vectores=0)
            res["replica"] = self._replicar(unidad, funciones, personal, padre)
            return res

        self._escribir_raw(doc_id, ficha)
        chunks = _chunkear(ficha, doc_id)
        if not chunks:
            return self._borrar(unidad.get("id"))

        self._retirar_del_indice(conn, doc_id)
        _insertar_chunks(conn, doc_id, chunks, h_nuevo)
        n_vec = self._embeber(chunks, doc_id)
        conn.commit()
        self._sucio += 1
        return ResultadoIngesta(
            accion="upsert", doc_id=doc_id, sin_cambios=False,
            chunks=len(chunks), vectores=n_vec,
            replica=self._replicar(unidad, funciones, personal, padre))

    def _borrar(self, unidad_id):
        doc_id = exportador.doc_id_de_unidad(unidad_id)
        conn = self.conexion()
        n_lapidas = self._retirar_del_indice(conn, doc_id)
        conn.execute("DELETE FROM docs_meta WHERE doc_id = ?", (doc_id,))
        conn.commit()
        ruta = self.raw / f"{doc_id}.json"
        existia = ruta.exists()
        if existia:
            ruta.unlink()
        if n_lapidas:
            self._sucio += 1
        res = ResultadoIngesta(accion="delete", doc_id=doc_id,
                               sin_cambios=not (existia or n_lapidas),
                               chunks=0, lapidas=n_lapidas)
        try:
            res["replica"] = ("borrada" if replica.borrar_unidad(
                self.replica(), unidad_id) else "no estaba")
        except Exception as exc:
            res["replica"] = f"error: {exc}"
        return res

    def _replicar(self, unidad, funciones, personal, padre) -> str:
        """Vuelca la unidad a la replica estructurada.

        Un fallo aqui NO tumba la ingesta: la ficha ya quedo indexada y el chat
        puede responder con ella. Se reporta en el resultado y la unidad queda
        sin fila, que es la forma en que la reconciliacion lo detecta y lo
        arregla en la pasada siguiente.
        """
        try:
            replica.guardar_unidad(self.replica(), unidad, funciones,
                                   personal, padre)
            return "ok"
        except Exception as exc:
            return f"error: {exc}"

    def _escribir_raw(self, doc_id: str, ficha: dict):
        """Escritura atomica: tmp + replace. Un JSON a medio escribir haria
        que el pipeline siguiente saltara el documento por JSON_ERROR."""
        self.raw.mkdir(parents=True, exist_ok=True)
        destino = self.raw / f"{doc_id}.json"
        tmp = destino.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ficha, f, ensure_ascii=False)
        os.replace(tmp, destino)

    def _retirar_del_indice(self, conn, doc_id: str) -> int:
        """Borra los chunks del documento de SQLite y pone lapida en la
        metadata de FAISS. Devuelve cuantas lapidas puso."""
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM chunks_meta WHERE doc_id = ?", (doc_id,))
        n = 0
        for m in self.motor.faiss_meta:
            if m.get("doc_id") == doc_id and not m.get("borrado"):
                m["borrado"] = True
                n += 1
        return n

    def _embeber(self, chunks, doc_id: str) -> int:
        """Embebe los chunks nuevos y los añade al indice vivo."""
        return self._agregar_vectores(chunks, self._vectores(chunks), doc_id)

    def _vectores(self, chunks):
        """Los vectores de los chunks, sin tocar el indice (la parte lenta)."""
        import numpy as np

        textos = [c["texto"] for c in chunks]
        if getattr(self.motor, "requiere_e5", False):
            textos = ["passage: " + t for t in textos]
        return self.motor.modelo_emb.encode(
            textos,
            batch_size=min(32, len(textos)),
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        ).astype(np.float32)

    def _agregar_vectores(self, chunks, vecs, doc_id: str) -> int:
        """Añade vectores ya calculados al indice vivo.

        La posicion en faiss_meta DEBE seguir coincidiendo con la fila del
        vector en FAISS: por eso se añade al indice y a la metadata en el mismo
        orden y en el mismo paso.
        """
        base = len(self.motor.faiss_meta)
        self.motor.faiss_index.add(vecs)
        for i, c in enumerate(chunks):
            self.motor.faiss_meta.append({
                "pos": base + i,
                "chunk_id": c["id"],
                "doc_id": doc_id,
                "hash_texto": _hash(c["texto"]),
            })
        return len(chunks)

    def preparar(self, doc_id: str, doc: dict) -> dict:
        """Chunks y vectores de un raw doc, SIN tocar la base ni el indice.

        Es la parte lenta (embeber en CPU) y se hace fuera de cualquier lock:
        el chat no tiene que esperar mientras se calculan los vectores."""
        if not self.disponible:
            raise MotorNoListo("el motor todavia no termino de cargar")
        from gaceta_rag import generar_base as gb

        chunks = _chunkear(doc, doc_id)
        return {
            "doc_id": doc_id,
            "chunks": chunks,
            "vectores": self._vectores(chunks) if chunks else None,
            "huella": gb.hash_contenido(doc.get("contenido") or ""),
        }

    def indexar(self, preparado: dict, doc: dict) -> ResultadoIngesta:
        """Escribe el raw y reemplaza chunks y vectores del documento.

        Orden pensado para una caida a mitad: raw -> SQLite (commit) -> indice
        en memoria. Si se corta despues del raw, la huella de docs_meta no
        coincide con la del raw; si se corta despues de SQLite, los vectores
        vivos no coinciden con los chunks (desfasados()). Las dos cosas las
        detecta la reparacion al arrancar y reindexa."""
        with self._lock:
            if not self.disponible:
                raise MotorNoListo("el motor todavia no termino de cargar")
            doc_id = preparado["doc_id"]
            chunks = preparado["chunks"]
            if not chunks:
                raise ValueError(f"{doc_id}: el documento no genera fragmentos")
            conn = self.conexion()
            self._escribir_raw(doc_id, doc)
            n_lapidas = self._retirar_del_indice(conn, doc_id)
            _insertar_chunks(conn, doc_id, chunks, preparado["huella"])
            conn.commit()
            n_vec = self._agregar_vectores(chunks, preparado["vectores"], doc_id)
            self._sucio += 1
            return ResultadoIngesta(accion="upsert", doc_id=doc_id,
                                    sin_cambios=False, chunks=len(chunks),
                                    vectores=n_vec, lapidas=n_lapidas)

    def retirar(self, doc_id: str, borrar_raw: bool = True) -> ResultadoIngesta:
        """Saca un documento de la base viva (chunks, huella y vectores)."""
        with self._lock:
            conn = self.conexion()
            n_lapidas = self._retirar_del_indice(conn, doc_id)
            conn.execute("DELETE FROM docs_meta WHERE doc_id = ?", (doc_id,))
            conn.commit()
            ruta = self.raw / f"{doc_id}.json"
            existia = ruta.exists()
            if borrar_raw and existia:
                ruta.unlink()
            if n_lapidas:
                self._sucio += 1
            return ResultadoIngesta(accion="delete", doc_id=doc_id,
                                    sin_cambios=not (existia or n_lapidas),
                                    chunks=0, lapidas=n_lapidas)

    def huella_de(self, doc_id: str):
        """Huella con la que quedo indexado el documento (docs_meta), o None."""
        with self._lock:
            return _huella_indexada(self.conexion(), doc_id)

    def doc_con_huella(self, huella: str):
        """Algun doc_id ya indexado con esa huella de contenido, o None."""
        with self._lock:
            fila = self.conexion().execute(
                "SELECT doc_id FROM docs_meta WHERE hash = ? LIMIT 1",
                (huella,)).fetchone()
            return fila[0] if fila else None

    def desfasados(self, doc_ids) -> set:
        """Los doc_ids cuyos vectores vivos no son los de sus chunks actuales.

        Generaliza _docs_desfasados() (que solo mira fichas de MOF) a
        cualquier documento, consultando de a uno: es para una lista corta
        (lo cargado a mano), no para el corpus entero."""
        doc_ids = list(doc_ids)
        if not doc_ids:
            return set()
        with self._lock:
            meta = getattr(self.motor, "faiss_meta", None) or []
            conn = self.conexion()
            esperado = {}
            for d in doc_ids:
                for chunk_id, texto in conn.execute(
                        "SELECT chunk_id, texto_emb FROM chunks_meta "
                        "WHERE doc_id = ?", (d,)):
                    esperado.setdefault(d, set()).add((chunk_id, _hash(texto)))
            buscados = set(doc_ids)
            vivos = {}
            for m in meta:
                d = m.get("doc_id")
                if d in buscados and not m.get("borrado"):
                    vivos.setdefault(d, set()).add(
                        (m.get("chunk_id"), m.get("hash_texto")))
            return {d for d in doc_ids
                    if esperado.get(d, set()) != vivos.get(d, set())}

    def resumen_doc(self, doc_id: str) -> dict:
        """Cuanto hay de un documento en cada mitad de la base."""
        with self._lock:
            conn = self.conexion()
            n_meta = conn.execute("SELECT COUNT(*) FROM chunks_meta WHERE "
                                  "doc_id = ?", (doc_id,)).fetchone()[0]
            n_fts = conn.execute("SELECT COUNT(*) FROM chunks WHERE doc_id = ?",
                                 (doc_id,)).fetchone()[0]
            meta = getattr(self.motor, "faiss_meta", None) or []
            vivos = sum(1 for m in meta
                        if m.get("doc_id") == doc_id and not m.get("borrado"))
            return {"chunks": n_meta, "chunks_fts": n_fts,
                    "vectores_vivos": vivos,
                    "huella": _huella_indexada(conn, doc_id)}

    def persistir_si_hace_falta(self) -> dict:
        """Baja el indice a disco si hay cambios y ya toca.

        _quiza_persistir() solo se evalua al aplicar un evento: si llegan tres
        cambios y despues nada, el plazo de MOF_FLUSH_SEGUNDOS vence sin que
        nadie lo mire y esos vectores viven solo en memoria. La consulta
        periodica llama a esto en cada vuelta, asi el plazo se cumple."""
        with self._lock:
            if not self.disponible or self._sucio <= 0:
                return {"persistido": False, "motivo": "nada pendiente"}
            vencido = (time.time() - self._ultimo_flush) >= FLUSH_SEGUNDOS
            if self._sucio >= FLUSH_CADA or vencido:
                return self.persistir()
            return {"persistido": False, "motivo": "todavia no toca"}

    def _quiza_persistir(self):
        if self._sucio <= 0:
            return
        vencido = (time.time() - self._ultimo_flush) >= FLUSH_SEGUNDOS
        if self._sucio >= FLUSH_CADA or vencido:
            self.persistir()

    def persistir(self) -> dict:
        """Baja indice y metadata a disco. Escritura atomica en los dos: un
        indice a medias y una metadata desincronizada dejan la base inservible."""
        with self._lock:
            if not self.disponible:
                return {"persistido": False, "motivo": "motor no cargado"}
            import faiss

            tmp_faiss = self.ruta_faiss.with_suffix(".bin.tmp")
            faiss.write_index(self.motor.faiss_index, str(tmp_faiss))
            tmp_meta = self.ruta_meta.with_suffix(".json.tmp")
            with open(tmp_meta, "w", encoding="utf-8") as f:
                json.dump(self.motor.faiss_meta, f, ensure_ascii=False)
            os.replace(tmp_faiss, self.ruta_faiss)
            os.replace(tmp_meta, self.ruta_meta)

            n = self._sucio
            self._sucio = 0
            self._ultimo_flush = time.time()
            return {"persistido": True, "eventos": n,
                    "vectores": int(self.motor.faiss_index.ntotal)}

class MotorNoListo(RuntimeError):
    """El motor todavia no cargo indice/modelo: el evento hay que reintentarlo."""

def _hash(texto: str) -> str:
    import hashlib
    return hashlib.sha1((texto or "").encode("utf-8", "ignore")).hexdigest()[:12]

def _huella_indexada(conn, doc_id: str):
    fila = conn.execute("SELECT hash FROM docs_meta WHERE doc_id = ?",
                        (doc_id,)).fetchone()
    return fila[0] if fila else None

def _tiene_chunks(conn, doc_id: str) -> bool:
    fila = conn.execute("SELECT 1 FROM chunks_meta WHERE doc_id = ? LIMIT 1",
                        (doc_id,)).fetchone()
    return fila is not None

def _insertar_chunks(conn, doc_id: str, chunks, huella_doc: str):
    from datetime import datetime
    for ch in chunks:
        conn.execute("INSERT OR REPLACE INTO chunks VALUES (?,?,?)",
                     (ch["id"], ch["doc_id"], ch["texto_puro"]))
        conn.execute("INSERT OR REPLACE INTO chunks_meta VALUES (?,?,?,?)",
                     (ch["id"], ch["doc_id"], ch["chunk_index"], ch["texto"]))
    conn.execute(
        "INSERT OR REPLACE INTO docs_meta (doc_id, hash, n_chunks, actualizado) "
        "VALUES (?,?,?,?)",
        (doc_id, huella_doc, len(chunks),
         datetime.now().isoformat(timespec="seconds")))

def _chunkear(ficha: dict, doc_id: str) -> list:
    """Mismo troceado que el pipeline: se reutilizan las funciones de
    generar_base para que un chunk ingerido en caliente sea indistinguible de
    uno construido en frio (misma cabecera, mismo tamaño, misma limpieza)."""
    from gaceta_rag import generar_base as gb

    limpio = gb.limpiar_ocr_seguro(ficha.get("contenido") or "")
    if not limpio or len(limpio) < gb.CHUNK_MIN_LEN:
        return []
    return gb.dividir_chunks(limpio, gb.cabecera_enriquecida(ficha), doc_id)
