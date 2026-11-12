# -*- coding: utf-8 -*-
"""
El puente: lo que entra por Paperless pasa a la carga manual de la Gaceta.

El webhook solo AVISA que un documento cambio. Nunca se usa lo que trae: se le
vuelve a preguntar a Paperless (si dos avisos se cruzan gana lo que Paperless
dice ahora) y el trabajo pesado va por una cola en disco, en un solo hilo. Asi
un aviso responde enseguida y sobrevive a un reinicio.

Todo entra por Cargador.crear_desde_archivo, igual que un archivo subido a mano:
mismo OCR, misma revision, mismo rango de ids (doc_1xxxxx). El id de Paperless
NO es el doc_id: un Paperless nuevo numera desde 1 y doc_00001 es del corpus.
Queda aparte, en raw['paperless']['id'].

Resultados de un aviso, en el registro:

  cargado     se creo su trabajo.
  ignorado    no lleva la etiqueta. Si se la ponen despues, llega otro aviso.
  no_visible  404 tras varios intentos: puede no existir o faltar el permiso.
  rechazado   la carga no lo acepta (formato, tamano). No traba a los demas.

  baja        salio de Paperless o le quitaron la etiqueta (reconciliar.py).

Un documento ya cargado que cambia NO crea otro trabajo:

  - Si solo cambio lo de Paperless, se aplica lo que cambio ALLI desde la ultima
    vez, y nada mas. Paperless manda en sus datos, pero una correccion de OCR o
    de titulo hecha en /carga sobrevive mientras no se cambie ese dato alla.
  - Si cambio el archivo, vuelve a revision con el mismo doc_id. Hasta que alguien
    lo confirme, el chat sigue con el texto anterior.
  - Lo que esta en proceso o en revision espera (`cambio_pendiente`).
  - Lo que se descarto en revision no vuelve. Para darle otra oportunidad: quitar
    la etiqueta en Paperless y, despues de la baja, volver a ponerla.
"""
import hashlib
import os
import threading
from datetime import datetime
from pathlib import Path

from gaceta_rag.carga import extraccion, ids
from gaceta_rag.carga.cargador import (Conflicto, ErrorCarga, NoEncontrado,
                                       _escribir_json)

from . import cliente as mod_cliente

ETIQUETA = "gaceta"
MAX_INTENTOS = 5
REINTENTO_S = 30.0
AYUDA_SIN_NOMBRE = "Corre docker/preparar_paperless.py y vuelve a revisar este documento."

def _ahora() -> str:
    return datetime.now().isoformat(timespec="seconds")

def _bool_env(nombre, defecto) -> bool:
    valor = os.environ.get(nombre)
    if valor is None or not valor.strip():
        return defecto
    return valor.strip().lower() in ("1", "true", "si", "sí", "on", "yes")

class Puente:
    def __init__(self, cliente, cargador, carpeta=None, etiqueta=None,
                 revisar=None, max_intentos=MAX_INTENTOS, reintento_s=None):
        self.cliente = cliente
        self.cargador = cargador
        self.carpeta = (Path(carpeta) if carpeta
                        else Path(cargador.raw).parent / "paperless")
        self.etiqueta = (etiqueta or os.environ.get("PAPERLESS_ETIQUETA")
                         or ETIQUETA).strip()
        self.revisar = (revisar if revisar is not None
                        else _bool_env("PAPERLESS_REVISAR", True))
        self.max_intentos = max_intentos
        self.reintento_s = (reintento_s if reintento_s is not None
                            else float(os.environ.get("PAPERLESS_REINTENTO_S")
                                       or REINTENTO_S))
        self._lock = threading.RLock()
        self._lock_proceso = threading.Lock()
        self._despertar = threading.Event()
        self._hilo = None
        self.ultima_reconciliacion = None
        self._estado = self._leer()

    @property
    def _ruta(self) -> Path:
        return self.carpeta / "estado.json"

    def _leer(self) -> dict:
        import json
        base = {"documentos": {}, "pendientes": {}, "registro": {},
                "ultimo_error": None, "bajas_retenidas": []}
        try:
            with open(self._ruta, encoding="utf-8") as f:
                base.update(json.load(f))
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"[paperless] estado ilegible, se empieza de cero: {exc}")
        return base

    def _guardar(self) -> None:
        _escribir_json(self._ruta, self._estado)

    def recibir_evento(self, pid: int) -> None:
        """Solo lo anota. No sale a la red: el webhook espera la respuesta."""
        with self._lock:
            self._estado["pendientes"].setdefault(
                str(int(pid)), {"recibido": _ahora(), "intentos": 0})
            self._guardar()
        self._despertar.set()

    def iniciar(self) -> None:
        if self._hilo is not None or not self.cliente.configurado:
            return
        self._hilo = threading.Thread(target=self._bucle, daemon=True,
                                      name="paperless-puente")
        self._hilo.start()

    def _bucle(self) -> None:
        while True:
            self._despertar.wait(timeout=self.reintento_s)
            self._despertar.clear()
            try:
                self.procesar_pendientes()
            except Exception as exc:
                print(f"[paperless] {type(exc).__name__}: {exc}")

    def procesar_pendientes(self) -> None:
        """Una pasada por lo pendiente. Publico para los tests (sin hilo)."""
        with self._lock_proceso:
            with self._lock:
                pids = [int(p) for p in self._estado["pendientes"]]
            for pid in pids:
                self._procesar_uno(pid)

    def _procesar_uno(self, pid: int) -> None:
        try:
            self._procesar(pid)
            with self._lock:
                self._estado["ultimo_error"] = None
                self._guardar()
        except mod_cliente.ArchivoDemasiadoGrande as exc:
            self._terminar(pid, "rechazado", str(exc))
        except mod_cliente.ErrorPaperless as exc:
            with self._lock:
                self._estado["ultimo_error"] = {"cuando": _ahora(), "doc": pid,
                                                "detalle": str(exc)}
                self._guardar()

    def _terminar(self, pid, resultado, motivo=None, modified=None) -> None:
        with self._lock:
            self._estado["pendientes"].pop(str(pid), None)
            self._estado["registro"][str(pid)] = {
                "resultado": resultado, "motivo": motivo, "cuando": _ahora(),
                "modified": modified}
            self._guardar()

    def _no_visible(self, pid) -> None:
        with self._lock:
            pendiente = self._estado["pendientes"].get(str(pid))
            if pendiente is None:
                return
            pendiente["intentos"] += 1
            if pendiente["intentos"] < self.max_intentos:
                self._guardar()
                return
        self._terminar(pid, "no_visible",
                       f"Paperless responde 404 tras {self.max_intentos} intentos")

    def _procesar(self, pid: int) -> None:
        doc = self.cliente.documento(pid)
        if doc is None:
            return self._no_visible(pid)

        with self._lock:
            previo = self._estado["documentos"].get(str(pid))
        if previo is not None:
            self.aplicar_cambio(pid, doc)
            return self._terminar(pid, "cargado", modified=doc.get("modified"))

        modified = doc.get("modified")
        nombres, sin_nombre = self._nombres_de_etiquetas(doc)
        if not self._lleva_etiqueta(nombres):
            motivo = f"no lleva la etiqueta {self.etiqueta}"
            if sin_nombre:
                motivo += f" (etiquetas sin nombre: {sin_nombre}; {AYUDA_SIN_NOMBRE})"
            return self._terminar(pid, "ignorado", motivo, modified)

        nombre = self._nombre_real(doc)
        existente = self.cargador.trabajo_de_origen("paperless", pid)
        if existente:
            datos = self._datos_de_carga(doc, nombre, nombres, sin_nombre)
            self._anotar(pid, doc, self.cargador.trabajo(existente), datos)
            return self._terminar(pid, "cargado", modified=modified)

        tipo = extraccion.tipo_de_archivo(nombre)
        if tipo is None or tipo == "json":
            return self._terminar(pid, "rechazado",
                                  f"{nombre}: formato que la carga no acepta", modified)

        descarga = self.cliente.original(pid)
        if descarga is None:
            return self._no_visible(pid)

        datos = self._datos_de_carga(doc, nombre, nombres, sin_nombre)
        try:
            t, = self.cargador.crear_desde_archivo(nombre, descarga[1], **datos)
        except ErrorCarga as exc:
            return self._terminar(pid, "rechazado", str(exc), modified)
        self._anotar(pid, doc, t, datos)
        self._terminar(pid, "cargado", modified=modified)

    def _lleva_etiqueta(self, nombres) -> bool:
        return any(n.casefold() == self.etiqueta.casefold() for n in nombres)

    @staticmethod
    def _nombre_real(doc) -> str:
        return doc.get("original_file_name") or f"documento-{doc['id']}"

    @staticmethod
    def _meta(datos) -> dict:
        return {k: datos.get(k) for k in ("titulo", "tipo_documento", "etiquetas",
                                          "raw_extra")}

    def _anotar(self, pid, doc, t, datos) -> None:
        with self._lock:
            self._estado["documentos"][str(pid)] = {
                "trabajo": t["id"], "doc_id": t.get("doc_id"),
                "modified": doc.get("modified"), "archivo_visto": doc.get("modified"),
                "sha256": (t.get("archivo") or {}).get("sha256"),
                "meta": self._meta(datos), "cargado": _ahora(),
                "cambio_pendiente": False, "aviso": None}
            self._guardar()

    def _situacion(self, d) -> tuple:
        """-> (situacion, trabajo o None). Situacion: 'indexado' (esta en el
        chat), el estado del trabajo si esta en camino, o 'descartado'."""
        t = None
        try:
            t = self.cargador.trabajo(d["trabajo"])
        except NoEncontrado:
            pass
        if t and t.get("doc_id"):
            d["doc_id"] = t["doc_id"]
        doc_id = d.get("doc_id")
        en_el_chat = bool(doc_id) and (self.cargador.raw / f"{doc_id}.json").exists()
        if t is None:
            return ("indexado" if en_el_chat else "descartado"), None
        return t["estado"], t

    def _archivo_nuevo(self, pid, doc, d):
        """Los bytes si el archivo cambio, o None. Sin pista de que cambio hay
        que bajarlo; pero una vez por cada `modified`, no en cada pasada."""
        if doc.get("modified") == d.get("archivo_visto"):
            return None
        descarga = self.cliente.original(pid)
        if descarga is None:
            return None
        d["archivo_visto"] = doc.get("modified")
        datos = descarga[1]
        return None if hashlib.sha256(datos).hexdigest() == d.get("sha256") else datos

    def aplicar_cambio(self, pid: int, doc: dict) -> str:
        """-> 'metadatos', 'archivo_nuevo', 'sin_cambios', 'espera' o 'descartado'.
        Hay que tener _lock_proceso (o ser el unico hilo)."""
        with self._lock:
            d = self._estado["documentos"][str(pid)]
        if doc.get("modified") == d.get("modified"):
            return "sin_cambios"
        situacion, t = self._situacion(d)
        if situacion == "descartado":
            d.update(modified=doc.get("modified"), cambio_pendiente=False)
            self._guardar_con_lock()
            return "descartado"
        if situacion not in ("indexado", "revision"):
            return self._esperar(d)

        nombre = self._nombre_real(doc)
        nombres, sin_nombre = self._nombres_de_etiquetas(doc)
        datos = self._datos_de_carga(doc, nombre, nombres, sin_nombre)
        nuevo = self._archivo_nuevo(pid, doc, d)
        if nuevo is not None:
            return self._reemplazar_archivo(d, doc, t, nombre, nuevo, datos)
        if situacion == "revision":
            return self._esperar(d)
        return self._aplicar_metadatos(d, doc, datos, sin_nombre)

    def _esperar(self, d) -> str:
        d["cambio_pendiente"] = True
        self._guardar_con_lock()
        return "espera"

    def _guardar_con_lock(self) -> None:
        with self._lock:
            self._guardar()

    def _reemplazar_archivo(self, d, doc, t, nombre, bytes_nuevos, datos) -> str:
        try:
            if t is not None and t["estado"] == "revision":
                self.cargador.descartar(t["id"])
            nuevo, = self.cargador.crear_desde_archivo(
                nombre, bytes_nuevos, doc_id=d.get("doc_id"), **datos)
        except (ErrorCarga, Conflicto) as exc:
            d.update(aviso=str(exc), cambio_pendiente=True, archivo_visto=None)
            self._guardar_con_lock()
            return "espera"
        d.update(trabajo=nuevo["id"], modified=doc.get("modified"),
                 sha256=(nuevo.get("archivo") or {}).get("sha256"),
                 meta=self._meta(datos), cambio_pendiente=False, aviso=None)
        self._guardar_con_lock()
        return "archivo_nuevo"

    def _aplicar_metadatos(self, d, doc, datos, etiquetas_sin_nombre) -> str:
        previa = d.get("meta") or {}
        cambios = {}
        meta = dict(previa)
        titulo, tipo = datos["titulo"], datos["tipo_documento"]
        if titulo is not None and titulo != previa.get("titulo"):
            cambios["titulo"] = meta["titulo"] = titulo
        if tipo is not None and tipo != previa.get("tipo_documento"):
            cambios["tipo_documento"] = meta["tipo_documento"] = tipo
        if (not etiquetas_sin_nombre
                and (datos["etiquetas"] or []) != (previa.get("etiquetas") or [])):
            cambios["etiquetas"] = datos["etiquetas"] or []
            meta["etiquetas"] = datos["etiquetas"]
        if datos["raw_extra"] != previa.get("raw_extra"):
            cambios["raw_extra"] = meta["raw_extra"] = datos["raw_extra"]
        aviso = " ".join(a["mensaje"] for a in datos["avisos_origen"]) or None

        if cambios:
            try:
                self.cargador.actualizar(d["doc_id"], enriquecer=False, **cambios)
            except (ErrorCarga, Conflicto, NoEncontrado) as exc:
                d.update(aviso=str(exc), cambio_pendiente=True)
                self._guardar_con_lock()
                return "espera"
        d.update(modified=doc.get("modified"), meta=meta, aviso=aviso,
                 cambio_pendiente=False)
        self._guardar_con_lock()
        return "metadatos" if cambios else "sin_cambios"

    def dar_baja(self, pid: int) -> None:
        """Lo saca del chat. Lanza Conflicto si esta en proceso, y ErrorCarga si
        lo que hay anotado no es de la carga manual: el corpus nunca se toca,
        ni con el mapa roto. Hay que tener _lock_proceso."""
        with self._lock:
            d = self._estado["documentos"][str(pid)]
        situacion, t = self._situacion(d)
        doc_id = d.get("doc_id")
        if doc_id and not ids.es_carga_manual(doc_id):
            raise ErrorCarga(f"{doc_id} no es de la carga manual: no se da de baja.")
        if situacion in ("en_cola", "extrayendo", "indexando"):
            raise Conflicto(f"esta en proceso ({situacion}): queda para la proxima pasada")
        if situacion == "revision" and t:
            self.cargador.descartar(t["id"])
        if doc_id and (self.cargador.raw / f"{doc_id}.json").exists():
            self.cargador.eliminar(doc_id)
        with self._lock:
            self._estado["documentos"].pop(str(pid), None)
            self._guardar()
        self._terminar(pid, "baja", "salio de Paperless o le quitaron la etiqueta")

    def fijar_retenidas(self, pids) -> None:
        with self._lock:
            self._estado["bajas_retenidas"] = sorted(pids)
            self._guardar()

    def _nombres_de_etiquetas(self, doc) -> tuple:
        nombres, sin_nombre = [], []
        for id_ in doc.get("tags") or []:
            nombre = self.cliente.nombre_etiqueta(id_)
            if nombre:
                nombres.append(nombre)
            else:
                sin_nombre.append(id_)
        return nombres, sin_nombre

    def _datos_de_carga(self, doc, nombre, etiquetas, etiquetas_sin_nombre) -> dict:
        avisos = []

        def falta(que, id_):
            avisos.append({
                "codigo": "paperless_sin_nombre",
                "mensaje": f"Paperless no deja leer el nombre de {que} {id_}: "
                           f"este dato puede estar incompleto. {AYUDA_SIN_NOMBRE}"})

        for id_ in etiquetas_sin_nombre:
            falta("la etiqueta", id_)
        tipo = self.cliente.nombre_tipo(doc.get("document_type"))
        if doc.get("document_type") is not None and not tipo:
            falta("el tipo de documento", doc["document_type"])
        corresponsal = self.cliente.nombre_corresponsal(doc.get("correspondent"))
        if doc.get("correspondent") is not None and not corresponsal:
            falta("el corresponsal", doc["correspondent"])

        campos = {}
        for campo in doc.get("custom_fields") or []:
            nombre_campo = self.cliente.nombre_campo(campo.get("field"))
            if nombre_campo:
                campos[nombre_campo] = campo.get("value")
            else:
                falta("el campo", campo.get("field"))

        extra = {"paperless": {
            "id": doc["id"], "created": doc.get("created"),
            "correspondent": corresponsal, "original_file_name": nombre}}
        if campos:
            extra["campos_personalizados"] = campos

        propias = [e for e in etiquetas
                   if e.casefold() != self.etiqueta.casefold()]
        titulo = (doc.get("title") or "").strip()
        return {
            "raw_extra": extra, "avisos_origen": avisos,
            "revisar": self.revisar or bool(avisos),
            "modo": "auto",
            "titulo": None if titulo == Path(nombre).stem else titulo,
            "tipo_documento": tipo,
            "etiquetas": propias or None,
        }

    def estado(self) -> dict:
        with self._lock:
            documentos = [dict(d, paperless_id=int(p)) for p, d
                          in self._estado["documentos"].items()]
            pendientes = [int(p) for p in self._estado["pendientes"]]
            registro = dict(self._estado["registro"])
            ultimo_error = self._estado["ultimo_error"]
            retenidas = list(self._estado["bajas_retenidas"])
        for d in documentos:
            d.pop("meta", None)
            d["doc_id"] = d["estado_trabajo"] = None
            try:
                t = self.cargador.trabajo(d["trabajo"])
                d["doc_id"], d["estado_trabajo"] = t.get("doc_id"), t.get("estado")
            except LookupError:
                d["estado_trabajo"] = "descartado"
        return {"configurado": self.cliente.configurado, "etiqueta": self.etiqueta,
                "revisar": self.revisar, "pendientes": pendientes,
                "documentos": documentos, "registro": registro,
                "ultimo_error": ultimo_error, "bajas_retenidas": retenidas,
                "ultima_reconciliacion": self.ultima_reconciliacion}
