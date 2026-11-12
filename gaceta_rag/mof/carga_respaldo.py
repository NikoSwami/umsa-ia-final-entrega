# -*- coding: utf-8 -*-
"""
Carga del RESPALDO de MOF desde la web: subir el pg_dump y que la Gaceta lo
incorpore sola, diciendo que entra, que cambia y —sobre todo— QUE SE VA.

Por que existe esto
───────────────────
respaldo.py ya sabia importar un pg_dump, pero desde la consola y con la API
detenida (el motor de la API tiene el indice en memoria y lo pisaria). En
Docker eso es incomodo: hay que entrar al contenedor, parar el servicio y
correr el comando. Y la importacion decia "borradas: 3" sin decir CUALES, asi
que una baja que no se esperaba pasaba de largo.

Aqui la importacion corre DENTRO del proceso de la API, con el mismo Ingestor
que la ingesta de MOF y la carga manual (el indice vivo tiene un solo
escritor), como una serie de pasos que quedan anotados:

  1. recibido            el archivo, su tamaño y su huella (sha256).
  2. leido               que sea un volcado de MOF; que trae; que esta dado
                         de baja DENTRO de MOF (deleted_at) y por eso no entra.
  3. comparado           el respaldo se importa a una replica de ENSAYO y se
                         compara con la actual: unidades nuevas, eliminadas y
                         modificadas campo por campo; funciones y cargos
                         agregados y quitados. Nada de lo real se toco aun.
  4. copia_de_seguridad  la replica actual se copia antes de tocarla.
  5. importado           replica + fichas del indice, unidad por unidad, por
                         el MISMO camino que un evento de MOF en vivo.
  6. verificado          la replica real tiene que quedar IGUAL a la de
                         ensayo, y ninguna ficha con vectores desfasados.

Valvula: si el respaldo se lleva demasiadas unidades, o es MAS VIEJO que los
datos que ya hay, el trabajo se detiene despues de comparar y espera que
alguien lo confirme mirando la lista de lo que se eliminaria. Un respaldo
cortado o equivocado es mucho mas frecuente que una reestructuracion que
borre media universidad.

El .sql NO se guarda: trae personas, usuarios y hashes de contraseñas. Se
borra en cuanto el trabajo termina (o se descarta); lo que queda es el informe.
"""
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import exportador, replica, respaldo

PASOS = ("recibido", "leido", "comparado", "copia_de_seguridad",
         "importado", "verificado")

CAMPOS = ("codigo", "sigla", "nombre", "objetivo", "padre_id", "padre_nombre",
          "tipo", "nivel", "relacion", "clase", "clase_id", "oficial",
          "res_creacion", "base_legal", "fec_creacion")

EN_CURSO = ("recibido", "procesando", "importando")
FINALES = ("listo", "simulado", "fallido", "descartado", "interrumpido")

MAX_MB = float(os.environ.get("MOF_RESPALDO_MAX_MB") or 200)
MAX_BAJAS_PCT = float(os.environ.get("MOF_RESPALDO_MAX_BAJAS_PCT") or 10)
COPIAS = int(os.environ.get("MOF_RESPALDO_COPIAS") or 10)
ESPERA_MOTOR_S = float(os.environ.get("MOF_RESPALDO_ESPERA_MOTOR_S") or 900)

class ErrorCargaRespaldo(RuntimeError):
    pass

class NoEncontrado(ErrorCargaRespaldo):
    pass

class Conflicto(ErrorCargaRespaldo):
    pass

def _ahora() -> str:
    return datetime.now().isoformat(timespec="seconds")

def foto(ruta) -> dict:
    """Lo que tiene una replica, en una forma comparable. Vacia si no existe."""
    vacia = {"unidades": {}, "funciones": {}, "cargos": {}, "origen_fecha": None}
    ruta = Path(ruta)
    if not ruta.exists():
        return vacia
    conn = replica.abrir_lectura(ruta)
    try:
        try:
            filas = conn.execute("SELECT * FROM unidad").fetchall()
        except sqlite3.DatabaseError:
            return vacia
        unidades = {f["id"]: {c: f[c] for c in CAMPOS} for f in filas}
        funciones, cargos = {}, {}
        for f in conn.execute("SELECT unidad_id, funcion FROM unidad_funcion "
                              "ORDER BY unidad_id, orden, id"):
            funciones.setdefault(f["unidad_id"], []).append(f["funcion"])
        for f in conn.execute("SELECT unidad_id, nombre FROM cargo_unidad "
                              "ORDER BY unidad_id, id"):
            cargos.setdefault(f["unidad_id"], []).append(f["nombre"])
        fila = conn.execute("SELECT valor FROM replica_meta "
                            "WHERE clave = 'origen_fecha'").fetchone()
        return {"unidades": unidades, "funciones": funciones, "cargos": cargos,
                "origen_fecha": fila[0] if fila else None}
    finally:
        conn.close()

def _quitados(antes: list, despues: list) -> list:
    """Lo de `antes` que no esta en `despues`, contando repetidos."""
    resto = list(despues)
    salida = []
    for x in antes:
        if x in resto:
            resto.remove(x)
        else:
            salida.append(x)
    return salida

def _ref(uid, datos) -> dict:
    return {"id": uid, "nombre": datos.get("nombre"), "sigla": datos.get("sigla"),
            "codigo": datos.get("codigo")}

def comparar(antes: dict, despues: dict) -> dict:
    """Que cambia al pasar de la replica `antes` a la `despues`.

    Lo ELIMINADO va con todo lo que se pierde: una unidad que se va se lleva
    sus funciones y sus cargos, y eso tambien se cuenta, porque "se fue 1
    unidad" esconde "y con ella 40 funciones"."""
    ua, ud = antes["unidades"], despues["unidades"]
    fa, fd = antes["funciones"], despues["funciones"]
    ca, cd = antes["cargos"], despues["cargos"]

    nuevas = [dict(_ref(u, ud[u]), funciones=len(fd.get(u, [])),
                   cargos=len(cd.get(u, [])))
              for u in sorted(set(ud) - set(ua))]
    eliminadas = [dict(_ref(u, ua[u]), funciones=len(fa.get(u, [])),
                       cargos=len(ca.get(u, [])), padre=ua[u].get("padre_nombre"))
                  for u in sorted(set(ua) - set(ud))]

    modificadas = []
    funciones_eliminadas, funciones_agregadas = [], []
    cargos_eliminados, cargos_agregados = [], []
    for u in sorted(set(ua) & set(ud)):
        cambios = {c: {"antes": ua[u].get(c), "despues": ud[u].get(c)}
                   for c in CAMPOS if ua[u].get(c) != ud[u].get(c)}
        quitadas = _quitados(fa.get(u, []), fd.get(u, []))
        puestas = _quitados(fd.get(u, []), fa.get(u, []))
        c_quitados = _quitados(ca.get(u, []), cd.get(u, []))
        c_puestos = _quitados(cd.get(u, []), ca.get(u, []))
        ref = _ref(u, ud[u])
        funciones_eliminadas += [dict(ref, funcion=f) for f in quitadas]
        funciones_agregadas += [dict(ref, funcion=f) for f in puestas]
        cargos_eliminados += [dict(ref, cargo=c) for c in c_quitados]
        cargos_agregados += [dict(ref, cargo=c) for c in c_puestos]
        if cambios or quitadas or puestas or c_quitados or c_puestos:
            modificadas.append(dict(ref, campos=cambios,
                                    funciones_eliminadas=len(quitadas),
                                    funciones_agregadas=len(puestas),
                                    cargos_eliminados=len(c_quitados),
                                    cargos_agregados=len(c_puestos)))

    def total(d):
        return sum(len(v) for v in d.values())

    return {
        "antes": {"unidades": len(ua), "funciones": total(fa), "cargos": total(ca)},
        "despues": {"unidades": len(ud), "funciones": total(fd), "cargos": total(cd)},
        "resumen": {
            "unidades_nuevas": len(nuevas),
            "unidades_eliminadas": len(eliminadas),
            "unidades_modificadas": len(modificadas),
            "funciones_agregadas": len(funciones_agregadas)
                                   + sum(n["funciones"] for n in nuevas),
            "funciones_eliminadas": len(funciones_eliminadas)
                                    + sum(e["funciones"] for e in eliminadas),
            "cargos_agregados": len(cargos_agregados)
                                + sum(n["cargos"] for n in nuevas),
            "cargos_eliminados": len(cargos_eliminados)
                                 + sum(e["cargos"] for e in eliminadas),
        },
        "unidades_nuevas": nuevas,
        "unidades_eliminadas": eliminadas,
        "unidades_modificadas": modificadas,
        "funciones_eliminadas": funciones_eliminadas,
        "funciones_agregadas": funciones_agregadas,
        "cargos_eliminados": cargos_eliminados,
        "cargos_agregados": cargos_agregados,
        "hay_eliminaciones": bool(eliminadas or funciones_eliminadas
                                  or cargos_eliminados),
        "sin_cambios": not (nuevas or eliminadas or modificadas),
    }

def bajas_en_mof(volcado) -> dict:
    """Lo que el respaldo trae DADO DE BAJA en MOF (deleted_at): no entra a la
    replica, y conviene verlo con nombre y fecha, no solo como un numero."""
    unidades = volcado.por_id("unidad", vivas=False)

    def nombre(uid):
        return (unidades.get(uid) or {}).get("nombre")

    por_unidad = {}
    for f in volcado.vivas("unidad_funcion"):
        por_unidad[f.get("unidad_id")] = por_unidad.get(f.get("unidad_id"), 0) + 1

    return {
        "unidades": [{"id": respaldo._num(f.get("id")), "nombre": f.get("nombre"),
                      "sigla": f.get("sigla"), "deleted_at": f.get("deleted_at"),
                      "funciones": por_unidad.get(f.get("id"), 0)}
                     for f in volcado.filas("unidad") if f.get("deleted_at")],
        "funciones": [{"id": respaldo._num(f.get("id")),
                       "unidad_id": respaldo._num(f.get("unidad_id")),
                       "unidad": nombre(f.get("unidad_id")),
                       "funcion": f.get("funcion"), "deleted_at": f.get("deleted_at")}
                      for f in volcado.filas("unidad_funcion") if f.get("deleted_at")],
        "cargos": sum(1 for f in volcado.filas("cargo_unidad") if f.get("deleted_at")),
    }

class _ConCandado:
    """El Ingestor, pero cada evento bajo el candado de quien comparte el
    indice (la carga manual y el motor mientras contesta).

    Por unidad y no por la importacion entera: tomar el lock del motor varios
    minutos dejaria el chat sin contestar todo ese rato."""

    def __init__(self, ingestor, candado):
        self._ingestor = ingestor
        self._candado = candado

    def aplicar(self, *a, **kw):
        with self._candado():
            return self._ingestor.aplicar(*a, **kw)

    def persistir(self, *a, **kw):
        with self._candado():
            return self._ingestor.persistir(*a, **kw)

    def __getattr__(self, nombre):
        return getattr(self._ingestor, nombre)

class CargadorRespaldo:
    """Recibe respaldos y los incorpora uno a la vez, en un hilo aparte.

    ingestor : el Ingestor de la API (el unico escritor del indice).
    carpeta  : donde quedan los trabajos, su informe y las copias de la replica.
    candado  : candado() -> context manager que se toma en cada evento.
    """

    def __init__(self, ingestor, carpeta, candado=None, max_bajas_pct=None,
                 espera_motor_s=None):
        self.ingestor = ingestor
        self.carpeta = Path(carpeta)
        self.candado = candado or contextlib.nullcontext
        self.max_bajas_pct = MAX_BAJAS_PCT if max_bajas_pct is None else max_bajas_pct
        self.espera_motor_s = (ESPERA_MOTOR_S if espera_motor_s is None
                               else espera_motor_s)
        self._lock = threading.RLock()
        self._uno_a_la_vez = threading.Lock()
        self._trabajos = {}
        self._hilos = {}
        self._cargar_previos()

    def _dir(self, tid) -> Path:
        return self.carpeta / tid

    def _guardar(self, t):
        d = self._dir(t["id"])
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "trabajo.json.tmp"
        tmp.write_text(json.dumps(t, ensure_ascii=False, indent=1, default=str),
                       encoding="utf-8")
        os.replace(tmp, d / "trabajo.json")

    def _cargar_previos(self):
        if not self.carpeta.exists():
            return
        for ruta in self.carpeta.glob("*/trabajo.json"):
            try:
                t = json.loads(ruta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if t.get("estado") in EN_CURSO:
                t["estado"] = "interrumpido"
                t["error"] = ("el servicio se reinicio a mitad del trabajo; "
                              "volver a subir el respaldo")
                self._borrar_sql(t)
                self._guardar(t)
            self._trabajos[t["id"]] = t

    def trabajos(self) -> list:
        with self._lock:
            ts = sorted(self._trabajos.values(), key=lambda t: t["creado"],
                        reverse=True)
            return [resumen(t) for t in ts]

    def trabajo(self, tid) -> dict:
        with self._lock:
            t = self._trabajos.get(tid)
            if t is None:
                raise NoEncontrado(f"no hay un trabajo {tid}")
            return json.loads(json.dumps(t, default=str))

    def estado(self) -> dict:
        ultimo = next((t for t in sorted(self._trabajos.values(),
                                         key=lambda t: t["creado"], reverse=True)
                       if t["estado"] == "listo"), None)
        return {"motor_listo": bool(getattr(self.ingestor, "disponible", False)),
                "replica": self.ingestor.estado_replica(),
                "ruta_replica": str(self.ingestor.ruta_replica),
                "max_mb": MAX_MB, "max_bajas_pct": self.max_bajas_pct,
                "en_curso": [t["id"] for t in self._trabajos.values()
                             if t["estado"] in EN_CURSO],
                "ultimo_listo": resumen(ultimo) if ultimo else None}

    def recibir(self, nombre: str, datos: bytes, simular: bool = False,
                permitir_bajas: bool = False) -> dict:
        if not datos:
            raise ErrorCargaRespaldo("el archivo llego vacio")
        tid = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        d = self._dir(tid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "respaldo.sql").write_bytes(datos)
        huella = hashlib.sha256(datos).hexdigest()
        t = {"id": tid, "archivo": Path(nombre or "respaldo.sql").name,
             "bytes": len(datos), "sha256": huella, "creado": _ahora(),
             "estado": "recibido", "simular": bool(simular),
             "permitir_bajas": bool(permitir_bajas), "eventos": [],
             "progreso": None, "error": None, "aviso": [], "informe": {}}
        igual = next((x for x in self._trabajos.values()
                      if x.get("sha256") == huella and x["estado"] == "listo"), None)
        if igual:
            t["aviso"].append(f"es el mismo archivo que ya se cargo en el "
                              f"trabajo {igual['id']}")
        self._evento(t, "recibido", "ok", f"{len(datos):,} bytes, sha256 {huella[:16]}")
        with self._lock:
            self._trabajos[tid] = t
            self._guardar(t)
        self._lanzar(tid, desde="leido")
        return self.trabajo(tid)

    def confirmar(self, tid) -> dict:
        """Sigue un trabajo que se detuvo por la valvula de bajas."""
        with self._lock:
            t = self._trabajos.get(tid)
            if t is None:
                raise NoEncontrado(f"no hay un trabajo {tid}")
            if t["estado"] != "esperando_confirmacion":
                raise Conflicto(f"el trabajo esta '{t['estado']}', no esperando "
                                f"confirmacion")
            if not (self._dir(tid) / "respaldo.sql").exists():
                raise Conflicto("el archivo del respaldo ya no esta: volver a subirlo")
            t["estado"] = "procesando"
            t["confirmado"] = _ahora()
            self._guardar(t)
        self._lanzar(tid, desde="copia_de_seguridad")
        return self.trabajo(tid)

    def descartar(self, tid) -> dict:
        with self._lock:
            t = self._trabajos.get(tid)
            if t is None:
                raise NoEncontrado(f"no hay un trabajo {tid}")
            if t["estado"] in EN_CURSO:
                raise Conflicto("el trabajo esta corriendo; esperar a que termine")
            if t["estado"] == "esperando_confirmacion":
                t["estado"] = "descartado"
                self._evento(t, "descartado", "ok", "no se toco la replica")
            self._borrar_sql(t)
            self._guardar(t)
            return resumen(t)

    def esperar(self, tid, timeout=None):
        """Para las pruebas: hasta que el hilo del trabajo termine."""
        hilo = self._hilos.get(tid)
        if hilo is not None:
            hilo.join(timeout)

    def _lanzar(self, tid, desde):
        hilo = threading.Thread(target=self._correr, args=(tid, desde),
                                daemon=True, name=f"respaldo-mof-{tid}")
        self._hilos[tid] = hilo
        hilo.start()

    def _evento(self, t, paso, estado, detalle=None, inicio=None):
        t["eventos"].append({"paso": paso, "estado": estado, "detalle": detalle,
                             "inicio": inicio or _ahora(), "fin": _ahora()})

    def _correr(self, tid, desde):
        t = self._trabajos[tid]
        sql = self._dir(tid) / "respaldo.sql"
        ensayo = self._dir(tid) / "ensayo.sqlite"
        paso = desde
        with self._uno_a_la_vez:
            try:
                with self._lock:
                    t["estado"] = "procesando"
                    self._guardar(t)
                if desde == "leido":
                    paso = "leido"
                    inicio = _ahora()
                    volcado = respaldo.leer(sql)
                    t["informe"]["respaldo"] = {
                        "fecha_datos": volcado.fecha,
                        "filas": {k: len(volcado.filas(k)) for k in respaldo.TABLAS
                                  if k in volcado.tablas},
                        "bajas_en_mof": bajas_en_mof(volcado),
                    }
                    b = t["informe"]["respaldo"]["bajas_en_mof"]
                    self._evento(t, "leido", "ok",
                                 f"datos al {volcado.fecha}; dados de baja en MOF: "
                                 f"{len(b['unidades'])} unidades (con "
                                 f"{sum(u['funciones'] for u in b['unidades'])} "
                                 f"funciones), {len(b['funciones'])} funciones "
                                 f"sueltas, "
                                 f"{b['cargos']} cargos", inicio)

                    paso = "comparado"
                    inicio = _ahora()
                    self._ensayar(sql, ensayo)
                    actual = foto(self.ingestor.ruta_replica)
                    prevista = foto(ensayo)
                    cmp = comparar(actual, prevista)
                    t["informe"]["comparacion"] = cmp
                    r = cmp["resumen"]
                    self._evento(t, "comparado", "ok",
                                 f"+{r['unidades_nuevas']} nuevas, "
                                 f"-{r['unidades_eliminadas']} eliminadas, "
                                 f"~{r['unidades_modificadas']} modificadas; "
                                 f"funciones +{r['funciones_agregadas']} "
                                 f"-{r['funciones_eliminadas']}", inicio)

                    motivos = self._motivos_para_confirmar(actual, prevista, cmp, volcado)
                    t["informe"]["motivos_confirmacion"] = motivos
                    if t["simular"]:
                        t["estado"] = "simulado"
                        self._evento(t, "simulado", "ok",
                                     "solo comparacion: no se toco la replica")
                        return
                    if motivos and not t["permitir_bajas"]:
                        t["estado"] = "esperando_confirmacion"
                        self._evento(t, "esperando_confirmacion", "detenido",
                                     "; ".join(motivos))
                        return

                prevista = foto(ensayo)

                paso = "copia_de_seguridad"
                inicio = _ahora()
                copia = self._copiar_replica(tid)
                t["informe"]["copia_replica"] = str(copia) if copia else None
                self._evento(t, "copia_de_seguridad", "ok",
                             str(copia) if copia else "no habia replica previa", inicio)

                paso = "importado"
                inicio = _ahora()
                self._esperar_motor(t)
                with self._lock:
                    t["estado"] = "importando"
                    self._guardar(t)

                def progreso(i, total):
                    t["progreso"] = {"hechas": i, "total": total}

                r = respaldo.importar(
                    sql, progreso=progreso,
                    ingestor=_ConCandado(self.ingestor, self.candado))
                t["informe"]["importacion"] = {
                    k: r.get(k) for k in ("unidades", "funciones", "cargos",
                                          "raices", "huerfanas", "importadas",
                                          "borradas", "fichas", "chunks",
                                          "persistido", "fecha", "cursor",
                                          "errores")}
                self._evento(t, "importado", "ok" if not r["errores"] else "con_errores",
                             f"{r['importadas']} unidades, {r['borradas']} borradas, "
                             f"{r['fichas']} fichas ({r['chunks']} chunks nuevos), "
                             f"indice en disco: {'si' if r['persistido'] else 'NO'}",
                             inicio)

                paso = "verificado"
                inicio = _ahora()
                v = self._verificar(prevista)
                t["informe"]["verificacion"] = v
                self._evento(t, "verificado", "ok" if v["ok"] else "error",
                             v["detalle"], inicio)
                if not v["ok"] or r["errores"]:
                    t["estado"] = "fallido"
                    t["error"] = (v["detalle"] if not v["ok"] else
                                  f"{len(r['errores'])} unidades con error")
                else:
                    t["estado"] = "listo"
            except Exception as exc:
                t["estado"] = "fallido"
                t["error"] = f"{type(exc).__name__}: {exc}"
                self._evento(t, paso, "error", t["error"])
            finally:
                t["terminado"] = _ahora() if t["estado"] in FINALES else None
                if t["estado"] != "esperando_confirmacion":
                    self._borrar_sql(t)
                with self._lock:
                    self._guardar(t)

    def _ensayar(self, sql, ensayo):
        """El respaldo importado a una replica aparte: la forma exacta en que
        va a quedar la real, con la misma normalizacion, sin tocarla."""
        for sufijo in ("", "-wal", "-shm"):
            Path(str(ensayo) + sufijo).unlink(missing_ok=True)
        respaldo.importar(sql, ruta=ensayo)

    def _motivos_para_confirmar(self, actual, prevista, cmp, volcado) -> list:
        motivos = []
        n_antes = len(actual["unidades"])
        n_bajas = cmp["resumen"]["unidades_eliminadas"]
        if n_antes and n_bajas * 100.0 / n_antes > self.max_bajas_pct:
            motivos.append(f"se eliminarian {n_bajas} de {n_antes} unidades "
                           f"(mas del {self.max_bajas_pct:g}%)")
        viejo, nuevo = actual.get("origen_fecha"), volcado.fecha
        if viejo and nuevo and nuevo < viejo:
            motivos.append(f"el respaldo es de {nuevo}, mas viejo que los datos "
                           f"actuales ({viejo})")
        return motivos

    def _copiar_replica(self, tid):
        origen = Path(self.ingestor.ruta_replica)
        if not origen.exists():
            return None
        destino = self._dir(tid) / "mof.antes.sqlite"
        fuente = sqlite3.connect(str(origen))
        try:
            copia = sqlite3.connect(str(destino))
            try:
                fuente.backup(copia)
            finally:
                copia.close()
        finally:
            fuente.close()
        self._podar_copias()
        return destino

    def _podar_copias(self):
        copias = sorted(self.carpeta.glob("*/mof.antes.sqlite"),
                        key=lambda p: p.parent.name, reverse=True)
        for vieja in copias[COPIAS:]:
            vieja.unlink(missing_ok=True)

    def _esperar_motor(self, t):
        limite = time.time() + self.espera_motor_s
        while not getattr(self.ingestor, "disponible", False):
            if time.time() > limite:
                raise ErrorCargaRespaldo(
                    "el motor no termino de cargar el indice a tiempo")
            t["progreso"] = {"esperando": "motor"}
            time.sleep(2)

    def _verificar(self, prevista) -> dict:
        real = foto(self.ingestor.ruta_replica)
        dif = comparar(prevista, real)
        docs = [exportador.doc_id_de_unidad(u) for u in real["unidades"]]
        desfasados = sorted(self.ingestor.desfasados(docs)) if docs else []
        sin_ficha = [d for d in docs if not (Path(self.ingestor.raw) / f"{d}.json").exists()]
        ok = dif["sin_cambios"] and not dif["hay_eliminaciones"] \
            and not desfasados and not sin_ficha
        if ok:
            detalle = (f"replica igual a la prevista ({len(real['unidades'])} "
                       f"unidades) y {len(docs)} fichas al dia en el indice")
        else:
            partes = []
            if not (dif["sin_cambios"] and not dif["hay_eliminaciones"]):
                partes.append(f"la replica difiere de la prevista: {dif['resumen']}")
            if desfasados:
                partes.append(f"{len(desfasados)} fichas con vectores desfasados")
            if sin_ficha:
                partes.append(f"{len(sin_ficha)} unidades sin ficha")
            detalle = "; ".join(partes)
        return {"ok": ok, "detalle": detalle, "desfasados": desfasados[:50],
                "sin_ficha": sin_ficha[:50]}

    def _borrar_sql(self, t):
        d = self._dir(t["id"])
        for nombre in ("respaldo.sql", "ensayo.sqlite", "ensayo.sqlite-wal",
                       "ensayo.sqlite-shm"):
            with contextlib.suppress(OSError):
                (d / nombre).unlink(missing_ok=True)

def resumen(t: dict) -> dict:
    """Un trabajo sin las listas largas del informe."""
    cmp = (t.get("informe") or {}).get("comparacion") or {}
    return {k: t.get(k) for k in ("id", "archivo", "bytes", "creado", "terminado",
                                  "estado", "simular", "progreso", "error", "aviso")} | {
        "resumen": cmp.get("resumen"),
        "fecha_datos": ((t.get("informe") or {}).get("respaldo") or {}).get("fecha_datos"),
        "ultimo_evento": (t.get("eventos") or [None])[-1],
    }
