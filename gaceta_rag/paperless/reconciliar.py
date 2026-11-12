# -*- coding: utf-8 -*-
"""
Reconciliacion con Paperless: la red para lo que el webhook no ve.

Paperless no tiene trigger de borrado, no avisa si le quitan la etiqueta a un
documento, y un aviso se pierde si la Gaceta esta arrancando. Cada
PAPERLESS_RECONCILIAR_CADA_S se compara el listado con lo anotado:

  no anotado          alta.
  otro `modified`     se aplica el cambio (ver puente.py).
  ya no aparece       baja, si se comprueba.

UN FALLO NUNCA PUEDE PARECER UN BORRADO. Por eso, antes de dar de baja:

  - El listado tiene que haber llegado entero (cliente.py): si falla o no cuadra,
    no se hace nada en toda la pasada.
  - Se vuelve a preguntar por ESE documento. Un 404, o que siga visible sin la
    etiqueta, es baja. Sin red, 401, 403 o 500 no lo es.
  - El 404 solo vale con otros documentos a la vista: un token sin permisos
    tambien recibe 404, y un Paperless con el volumen vacio tambien. Con el
    listado vacio, ninguna baja se aplica sola.

VALVULA. Muchas bajas juntas (mas de PAPERLESS_MINIMO_BAJAS y mas de
PAPERLESS_UMBRAL_BAJAS del total) se sospechan de la fuente. Quedan retenidas
hasta POST /api/paperless/reconciliar con {"confirmar": true}.

Solo se da de baja lo anotado, y nunca fuera del rango manual (puente.dar_baja).
"""
import os

from gaceta_rag.carga.cargador import Conflicto, ErrorCarga, NoEncontrado

from . import cliente as mod_cliente
from .puente import _ahora

UMBRAL_BAJAS = 0.25
MINIMO_BAJAS = 5

class Reconciliador:
    def __init__(self, puente, umbral=None, minimo=None):
        self.puente = puente
        self.umbral = (umbral if umbral is not None else
                       float(os.environ.get("PAPERLESS_UMBRAL_BAJAS") or UMBRAL_BAJAS))
        self.minimo = (minimo if minimo is not None else
                       int(os.environ.get("PAPERLESS_MINIMO_BAJAS") or MINIMO_BAJAS))
        self.ultima = None

    def _sospechoso(self, bajas: int, total: int) -> bool:
        return bajas > self.minimo and bajas / max(total, 1) > self.umbral

    def ejecutar(self, confirmar: bool = False) -> dict:
        """Una pasada. confirmar=True pasa por encima de la valvula, no de las
        comprobaciones: lo que no se pudo comprobar sigue sin borrarse."""
        p = self.puente
        r = {"ok": True, "motivo": None, "aviso": None, "altas": [], "cambios": [],
             "bajas": [], "retenidas": [], "errores": []}
        if not p.cliente.configurado:
            r.update(ok=False, motivo="Paperless no esta configurado")
            return r
        with p._lock_proceso:
            try:
                listado = {f["id"]: f.get("modified")
                           for f in p.cliente.listar_con_etiqueta(p.etiqueta)}
            except mod_cliente.ErrorPaperless as exc:
                r.update(ok=False, motivo=str(exc))
                self._anotar(r)
                return r
            with p._lock:
                anotados = {int(k): v for k, v in p._estado["documentos"].items()}
            self._altas(listado, anotados, r)
            self._cambios(listado, anotados, r)
            self._bajas(listado, anotados, r, confirmar)
        self._anotar(r)
        return r

    def _anotar(self, r) -> None:
        self.ultima = r
        self.puente.ultima_reconciliacion = {
            "cuando": _ahora(), "ok": r["ok"], "motivo": r["motivo"],
            "aviso": r["aviso"], "errores": len(r["errores"])}

    def _altas(self, listado, anotados, r) -> None:
        p = self.puente
        with p._lock:
            registro = dict(p._estado["registro"])
        for pid, modified in listado.items():
            if pid in anotados:
                continue
            previo = registro.get(str(pid)) or {}
            if (previo.get("resultado") in ("rechazado", "ignorado")
                    and previo.get("modified") == modified):
                continue
            p.recibir_evento(pid)
            p._procesar_uno(pid)
            r["altas"].append(pid)

    def _cambios(self, listado, anotados, r) -> None:
        p = self.puente
        for pid, d in anotados.items():
            if pid not in listado or listado[pid] == d.get("modified"):
                continue
            try:
                doc = p.cliente.documento(pid)
                if doc is None:
                    continue
                if p.aplicar_cambio(pid, doc) in ("metadatos", "archivo_nuevo"):
                    r["cambios"].append(pid)
            except mod_cliente.ErrorPaperless as exc:
                r["errores"].append({"doc": pid, "detalle": str(exc)})

    def _candidato(self, pid, r) -> bool:
        """Se comprueba de a uno: no aparecer en el listado no alcanza."""
        p = self.puente
        try:
            doc = p.cliente.documento(pid)
            if doc is None:
                return True
            nombres, sin_nombre = p._nombres_de_etiquetas(doc)
        except mod_cliente.ErrorPaperless as exc:
            r["errores"].append({"doc": pid, "detalle": str(exc)})
            return False
        if p._lleva_etiqueta(nombres):
            return False
        if sin_nombre:
            r["errores"].append({"doc": pid, "detalle":
                                 f"etiquetas sin nombre: {sin_nombre}"})
            return False
        return True

    def _bajas(self, listado, anotados, r, confirmar) -> None:
        p = self.puente
        candidatos = [pid for pid in sorted(anotados)
                      if pid not in listado and self._candidato(pid, r)]
        vacio = not listado and bool(anotados)
        if candidatos and not confirmar and (vacio or self._sospechoso(
                len(candidatos), len(anotados))):
            r["retenidas"] = candidatos
            r["aviso"] = (f"{len(candidatos)} bajas de {len(anotados)} documentos: "
                          "demasiadas para aplicarlas solas. Si es correcto, pedi "
                          'POST /api/paperless/reconciliar con {"confirmar": true}.')
            if vacio:
                r.update(ok=False, motivo="Paperless no lista ningun documento con "
                                          f"la etiqueta {p.etiqueta}")
            p.fijar_retenidas(candidatos)
            return
        p.fijar_retenidas([])
        for pid in candidatos:
            try:
                p.dar_baja(pid)
                r["bajas"].append(pid)
            except (ErrorCarga, Conflicto, NoEncontrado) as exc:
                r["errores"].append({"doc": pid, "detalle": str(exc)})
