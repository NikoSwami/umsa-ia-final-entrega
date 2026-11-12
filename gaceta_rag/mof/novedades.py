# -*- coding: utf-8 -*-
"""
La Gaceta PREGUNTA a MOF que cambio. Sola, cada pocos segundos.

Por que la Gaceta pregunta y no MOF avisa
─────────────────────────────────────────
MOF ya sabe avisar (GacetaSyncService, PR #60): cuando cambia una unidad, hace
un POST a la Gaceta. Funciona, pero pone la iniciativa del lado equivocado:

  - MOF tendria que guardar un secreto de la Gaceta y salir a buscarla por la
    red, y la Gaceta tendria que dejar abierta una puerta de escritura para
    que entre. Preguntando, MOF no guarda nada de la Gaceta ni sale a ningun
    lado, y la Gaceta no abre ninguna puerta: solo lee.
  - Si la Gaceta esta apagada cuando MOF avisa, el aviso espera en la bandeja
    de MOF. Preguntando, la Gaceta se pone al dia sola al volver, desde donde
    se quedo.
  - La base de la Gaceta es de la Gaceta: que se llene y se mantenga es
    responsabilidad suya, no de otro sistema que quiza ni este configurado.

A quien le pregunta
───────────────────
A la AUDITORIA de MOF (GET /versiones). Es la tabla auditoria_cambio, que MOF
escribe dentro de la MISMA transaccion que cada cambio de unidad o de funcion:
no puede haber un cambio sin su fila. Cada fila dice que unidad hay que
volver a leer (unidadAfectadaId), incluso cuando lo que cambio fue una
funcion.

Preguntar al LISTADO de unidades, como hace la reconciliacion, no alcanza: el
listado no trae funciones ni cargos, asi que un cambio en una funcion es
invisible ahi. La auditoria si lo ve, y en UNA peticion.

De cada fila se usa el id de la unidad y nada mas. Los datos del cambio que
trae la fila (datosNuevos) se ignoran a proposito: la unidad se vuelve a leer
de MOF, y gana lo que MOF dice AHORA y no el orden en que llegaron las filas.

Como no se pierde nada
──────────────────────
  - CURSOR: la fecha de la fila mas nueva ya procesada. Se guarda en la propia
    replica, asi sobrevive a un reinicio y, si la replica se reconstruye, se
    borra con ella y fuerza una carga completa.
  - SOLAPE: cada consulta vuelve a mirar unos minutos antes del cursor. MOF
    pone created_at al EMPEZAR la transaccion, no al confirmarla: un cambio
    lento puede aparecer despues con una fecha anterior a otro ya visto. Las
    filas repetidas por el solape se reconocen por su id y no se reaplican;
    esos ids se guardan con el cursor, asi un reinicio no relee la ventana.
  - El cursor solo avanza cuando todo se aplico. Si MOF deja de responder a
    mitad, la vuelta siguiente lo repite; reaplicar es seguro, porque una
    unidad que no cambio no se vuelve a embeber.

Todo es automatico
──────────────────
Nadie tiene que crear ni llenar nada a mano:

  - la primera vez (sin cursor) o con la replica vacia -> carga completa;
  - despues -> solo las unidades que la auditoria nombra;
  - una avalancha de cambios (un ETL, una importacion) -> una reconciliacion
    profunda, que sale mas barata que leer unidad por unidad;
  - sin la auditoria (un MOF que no la expone, o sin permiso para leerla) ->
    la reconciliacion rapida en cada vuelta: se sigue al dia, sin ver
    funciones hasta la pasada profunda. Se dice en el estado, no se calla.
    Sin ninguna cuenta (sin MOF_USUARIO), en cambio, no alcanza: desde
    AIVERKE/MOF fa691c6 MOF pide sesion tambien para leer las unidades, y la
    reconciliacion falla igual (cliente.aviso_sin_credenciales).

Lo que la auditoria NO ve: los cargos (cargo_unidad) y los catalogos, que el
subscriber de MOF no audita. Eso lo trae la reconciliacion periodica.
"""
import json
import os
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from .cliente import ErrorMOF

CLAVE_CURSOR = "mof_cursor_auditoria"
CLAVE_VISTAS = "mof_filas_vistas"
CURSOR_INICIAL = "1970-01-01T00:00:00.000Z"

SOLAPE_S = float(os.environ.get("MOF_SOLAPE_S") or "300")
LIMITE_PAGINA = 200
MAX_PAGINAS = int(os.environ.get("MOF_MAX_PAGINAS") or "10")
RECORDADOS = 5000
SIN_AUDITORIA = (401, 403, 404)
MINIMO_BAJAS_SOSPECHOSAS = int(os.environ.get("MOF_MINIMO_BAJAS") or "5")

class Consultor:
    """Consulta de novedades. Una instancia por proceso."""

    def __init__(self, cliente, ingestor, reconciliador):
        self.cliente = cliente
        self.ingestor = ingestor
        self.reconciliador = reconciliador
        self._vistas = None
        self._una_a_la_vez = threading.Lock()
        self.ultima = {}
        self.ultima_ok = {}

    def estado(self, cursor: bool = True) -> dict:
        """Lo que hizo la ultima consulta, mas cuando fue la ultima que salio
        bien (ultimo_ok, modo_ultimo_ok).

        cursor=False no toca la replica: el cursor se lee con la conexion de
        la ingesta, bajo su lock, y el chat —que pide el estado en cada
        pregunta— no puede quedarse esperando en medio de una carga."""
        salida = dict(self.ultima)
        salida["ultimo_ok"] = self.ultima_ok.get("cuando")
        salida["modo_ultimo_ok"] = self.ultima_ok.get("modo")
        if cursor and self.ingestor is not None and self.ingestor.disponible:
            salida["cursor"] = self.ingestor.leer_meta(CLAVE_CURSOR)
        return salida

    def consultar(self) -> dict:
        """Una vuelta: pregunta, aplica y devuelve el resumen. No lanza."""
        if not self._una_a_la_vez.acquire(blocking=False):
            return {"ok": False, "motivo": "ya hay una consulta en curso"}
        try:
            try:
                r = self._consultar()
            except Exception as exc:
                r = {"ok": False, "motivo": f"{exc.__class__.__name__}: {exc}"}
            r["cuando"] = _iso(datetime.now(timezone.utc))
            if r.get("ok"):
                self.ultima_ok = r
            self.ultima = r
            return r
        finally:
            self._una_a_la_vez.release()

    def _consultar(self) -> dict:
        if not self.cliente.configurado:
            return {"ok": False, "motivo": "MOF_URL no configurado"}
        if self.ingestor is None or not self.ingestor.disponible:
            return {"ok": False, "motivo": "el motor todavia no esta cargado"}

        cursor = self.ingestor.leer_meta(CLAVE_CURSOR)
        if cursor is None:
            return self._carga_completa("primera consulta")
        if self.ingestor.ids_en_replica() == set():
            return self._carga_completa("la replica esta vacia")
        return self._incremental(cursor)

    def _carga_completa(self, motivo: str) -> dict:
        modo, aviso = "auditoria", None
        try:
            tope = self._fila_mas_nueva()
        except ErrorMOF as exc:
            if exc.codigo not in SIN_AUDITORIA:
                return {"ok": False, "motivo": f"MOF inalcanzable: {exc}"}
            tope, modo = None, "sin_auditoria"
            aviso = _aviso_sin_auditoria(exc)

        r = self.reconciliador.ejecutar()
        if not r.get("ok"):
            return {"ok": False, "motivo": r.get("motivo"), "modo": modo}

        if modo == "auditoria":
            cursor = tope or CURSOR_INICIAL
            self._vistas = OrderedDict()
            try:
                self._marcar_vistas(self.cliente.cambios(
                    _restar(cursor, SOLAPE_S), pagina=1, limite=LIMITE_PAGINA),
                    cursor)
            except ErrorMOF:
                self._marcar_vistas([], cursor)
            self._guardar(cursor)
        self.ingestor.persistir_si_hace_falta()
        plan = r.get("plan") or {}
        return {
            "ok": True, "tipo": "carga_completa", "modo": modo,
            "motivo_carga": motivo,
            "aplicadas": r.get("aplicadas", 0),
            "reindexadas": r.get("reindexadas", 0),
            "bajas": r.get("bajas", 0),
            "errores": r.get("errores", []),
            "aviso": aviso or plan.get("aviso"),
        }

    def _fila_mas_nueva(self):
        """createdAt de la fila mas nueva de la auditoria, o None si esta vacia.
        MOF ordena de la mas nueva a la mas vieja: basta la primera."""
        filas = self.cliente.cambios(CURSOR_INICIAL, pagina=1, limite=1)
        return _fecha(filas[0]) if filas else None

    def _incremental(self, cursor: str) -> dict:
        desde = _restar(cursor, SOLAPE_S)
        filas, avalancha = [], False
        try:
            for pagina in range(1, MAX_PAGINAS + 1):
                lote = self.cliente.cambios(desde, pagina=pagina,
                                            limite=LIMITE_PAGINA)
                filas.extend(lote)
                if len(lote) < LIMITE_PAGINA:
                    break
            else:
                avalancha = True
        except ErrorMOF as exc:
            if exc.codigo in SIN_AUDITORIA:
                r = self.reconciliador.ejecutar()
                return {"ok": bool(r.get("ok")), "tipo": "reconciliacion",
                        "modo": "sin_auditoria",
                        "aplicadas": r.get("aplicadas", 0),
                        "reindexadas": r.get("reindexadas", 0),
                        "bajas": r.get("bajas", 0),
                        "errores": r.get("errores", []),
                        "aviso": _aviso_sin_auditoria(exc)}
            return {"ok": False, "motivo": f"MOF inalcanzable: {exc}",
                    "modo": "auditoria"}

        nuevo_cursor = _mas_nueva([cursor] + [_fecha(f) for f in filas])

        if avalancha:
            r = self.reconciliador.ejecutar(profundo=True)
            if not r.get("ok"):
                return {"ok": False, "motivo": r.get("motivo"),
                        "modo": "auditoria"}
            self._marcar_vistas(filas, nuevo_cursor)
            self._guardar(nuevo_cursor)
            self.ingestor.persistir_si_hace_falta()
            return {"ok": True, "tipo": "reconciliacion_profunda",
                    "modo": "auditoria", "cambios_leidos": len(filas),
                    "aplicadas": r.get("aplicadas", 0),
                    "reindexadas": r.get("reindexadas", 0),
                    "bajas": r.get("bajas", 0),
                    "errores": r.get("errores", []),
                    "aviso": (f"llegaron mas de {MAX_PAGINAS * LIMITE_PAGINA} "
                              f"cambios de golpe: se reviso todo el "
                              f"organigrama en vez de unidad por unidad")}

        unicas = OrderedDict()
        for f in filas:
            unicas[str(f.get("id"))] = f
        vistas = self._cargar_vistas()
        nuevas = [f for aid, f in unicas.items() if aid not in vistas]

        ultima_accion = OrderedDict()
        for f in sorted(nuevas, key=lambda f: (_fecha(f) or "", _num(f.get("id")))):
            uid = _num(f.get("unidadAfectadaId", f.get("unidad_afectada_id")))
            if uid is None:
                continue
            ultima_accion.pop(uid, None)
            ultima_accion[uid] = str(f.get("accion") or "").upper()

        leidas = OrderedDict()
        try:
            for uid in ultima_accion:
                leidas[uid] = self.cliente.unidad_completa(uid)
        except ErrorMOF as exc:
            return {"ok": False, "motivo": f"MOF inalcanzable leyendo la "
                                           f"unidad {uid}: {exc}",
                    "modo": "auditoria"}

        inesperadas = [uid for uid, datos in leidas.items()
                       if not datos and ultima_accion[uid] != "DELETE"]
        omitidas, aviso = [], None
        if len(inesperadas) >= MINIMO_BAJAS_SOSPECHOSAS:
            omitidas = inesperadas
            aviso = (f"{len(inesperadas)} unidades con cambios recientes ya no "
                     f"aparecen en MOF sin que la auditoria las haya borrado: "
                     f"no se dan de baja. La reconciliacion, que mira el "
                     f"listado completo, decidira.")

        aplicadas = reindexadas = bajas = 0
        errores = []
        for uid, datos in leidas.items():
            if uid in omitidas:
                continue
            try:
                res = self.reconciliador.aplicar_leida(uid, datos)
            except ValueError as exc:
                errores.append({"unidad": uid, "error": str(exc)})
                continue
            if not res:
                continue
            if res.get("accion") == "delete":
                bajas += 1
            else:
                aplicadas += 1
                if not res.get("sin_cambios"):
                    reindexadas += 1

        self._marcar_vistas(filas, nuevo_cursor)
        self._guardar(nuevo_cursor)
        self.ingestor.persistir_si_hace_falta()
        salida = {"ok": True, "tipo": "incremental", "modo": "auditoria",
                  "cambios_leidos": len(unicas), "nuevos": len(nuevas),
                  "unidades": len(ultima_accion), "aplicadas": aplicadas,
                  "reindexadas": reindexadas, "bajas": bajas,
                  "errores": errores, "aviso": aviso}
        if omitidas:
            salida["bajas_omitidas"] = omitidas
        return salida

    def _cargar_vistas(self) -> dict:
        if self._vistas is None:
            try:
                self._vistas = OrderedDict(json.loads(
                    self.ingestor.leer_meta(CLAVE_VISTAS) or "{}"))
            except (TypeError, ValueError):
                self._vistas = OrderedDict()
        return self._vistas

    def _marcar_vistas(self, filas, cursor: str):
        """Anota las filas y olvida las que ya quedaron fuera de la ventana:
        solo hace falta recordar lo que el solape puede volver a traer."""
        vistas = self._cargar_vistas()
        for f in filas:
            fecha = _fecha(f)
            if fecha:
                vistas[str(f.get("id"))] = fecha
        limite = _restar(cursor, SOLAPE_S)
        for aid in [a for a, fecha in vistas.items()
                    if _parse(fecha) < _parse(limite)]:
            del vistas[aid]
        while len(vistas) > RECORDADOS:
            vistas.popitem(last=False)

    def _guardar(self, cursor: str):
        self.ingestor.guardar_meta(CLAVE_VISTAS,
                                   json.dumps(self._cargar_vistas()))
        self.ingestor.guardar_meta(CLAVE_CURSOR, cursor)

def _parse(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None

def _iso(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"

def _restar(iso: str, segundos: float) -> str:
    dt = _parse(iso) or _parse(CURSOR_INICIAL)
    return _iso(dt - timedelta(seconds=segundos))

def _fecha(fila):
    dt = _parse((fila or {}).get("createdAt") or (fila or {}).get("created_at"))
    return _iso(dt) if dt else None

def _mas_nueva(fechas):
    validas = [d for d in (_parse(f) for f in fechas if f) if d]
    return _iso(max(validas)) if validas else CURSOR_INICIAL

def _num(valor):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None

def _aviso_sin_auditoria(exc: ErrorMOF) -> str:
    if exc.codigo == 404:
        return ("este MOF no expone su auditoria (GET /versiones -> 404): "
                "se usa la reconciliacion. Los cambios en funciones se ven "
                "recien en la pasada profunda.")
    return ("sin permiso para leer la auditoria de MOF "
            f"(GET /versiones -> {exc.codigo}): falta MOF_USUARIO/MOF_PASSWORD "
            "o no son validos. Se usa la reconciliacion; los cambios en "
            "funciones se ven recien en la pasada profunda.")
