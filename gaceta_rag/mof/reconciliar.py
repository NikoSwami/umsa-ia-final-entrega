# -*- coding: utf-8 -*-
"""
Reconciliacion: compara lo que MOF tiene AHORA con lo que la gaceta tiene
indexado, y arregla la diferencia.

POR QUE HACE FALTA SI YA HAY WEBHOOK
Porque un webhook siempre se pierde alguno. La bandeja de salida de MOF
reintenta, pero hay agujeros que ningun reintento tapa:

  - eventos abandonados tras agotar los intentos,
  - cambios hechos por fuera del ORM (SQL a mano, el seeder, el ETL): el
    subscriber de MOF no los ve y no genera evento,
  - la gaceta restaurada desde una copia mas vieja que MOF,
  - la ventana entre que MOF guarda y el evento se entrega, si el proceso
    muere justo ahi.

Ninguno de esos falla ruidosamente. Se manifiestan como un chat que responde
con el organigrama de hace tres semanas. La reconciliacion es lo que convierte
eso en un problema que se corrige solo.

DOS PASADAS, PORQUE EL LISTADO DE MOF NO TRAE TODO
GET /api/v1/mof/unidades devuelve nombre, sigla, codigo, objetivo, base legal,
resolucion de creacion, tipo, nivel, relacion y la unidad de la que depende —
pero NO trae `updatedAt` ni las funciones ni el personal.

  Pasada RAPIDA (por defecto): una sola peticion. Detecta altas, bajas y
  cambios en los campos propios de la unidad, comparando la huella de esos
  campos contra la que se guardo la ultima vez.

  Pasada PROFUNDA (--profundo): baja cada unidad entera. Es la unica que ve un
  cambio en una FUNCION o en el personal, porque esos viven en otras tablas y
  no asoman en el listado. Cuesta ~3 peticiones por unidad: es para una vez al
  dia, no para cada rato.

NO REINDEXA POR LAS DUDAS
Cuando la huella del listado no coincide, se baja la unidad completa y se le
pasa al Ingestor, que compara el texto real y no hace nada si no cambio
(`sin_cambios`). Asi un falso positivo cuesta una peticion, no un
re-embedding. Y la huella se vuelve a sellar SIEMPRE desde el listado, que es
lo que evita que una diferencia de formato entre endpoints deje a una unidad
divergiendo para siempre.

VALVULA DE SEGURIDAD
Si MOF responde con una lista vacia — un fallo de permisos que devuelve [] en
vez de un error, una base recien migrada — la lectura ingenua seria "borralo
todo". Por eso las bajas masivas se rechazan y se reportan en vez de
aplicarse; hace falta --forzar para pasar por encima.
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from . import exportador
from .cliente import ClienteMOF, ErrorMOF

UMBRAL_BAJAS = float(os.environ.get("MOF_UMBRAL_BAJAS") or "0.25")
MINIMO_PARA_SOSPECHAR = int(os.environ.get("MOF_MINIMO_BAJAS") or "5")

class Plan(dict):
    """Que hay que hacer. dict para devolverlo tal cual por HTTP."""

    @property
    def hay_trabajo(self) -> bool:
        return bool(self["altas"] or self["cambios"] or self["bajas"])

def huella_listado(item: dict) -> str:
    """Huella de los campos de una unidad que el LISTADO de MOF si trae.

    Se calcula sobre la proyeccion normalizada (no sobre el JSON crudo) para
    que dos formas distintas del mismo dato — 'tipo' como texto en el listado
    y como objeto en el detalle, una fecha con hora y otra sin ella — den la
    misma huella. Sin esa normalizacion, cada reconciliacion "encontraria"
    diferencias que no existen.
    """
    proyeccion = exportador.proyeccion_unidad(item)
    crudo = json.dumps(proyeccion, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(crudo.encode("utf-8", "ignore")).hexdigest()[:12]

def estado_local(raw=None, indexados=None, en_replica=None) -> dict:
    """unidad_id -> {'huella', 'indexado', 'en_replica'} de lo que hay hoy.

    `huella` es la del listado sellada en la ultima reconciliacion; None
    significa "esta ficha nunca se reconcilio" (llego por webhook o por el
    pipeline en frio) y obliga a verificarla.

    `indexados` es el conjunto de doc_ids con chunks (Ingestor.docs_indexados).
    Llega ya resuelto, no como una conexion: leer la base es cosa del Ingestor,
    que serializa sus accesos. Sin `indexados` no se comprueba ese lado.

    `en_replica` es lo mismo para la replica estructurada
    (Ingestor.ids_en_replica). Una unidad con ficha pero sin fila quedo a
    medias —fallo la escritura, o la replica se creo despues de que la ficha ya
    estuviera— y se marca para rehacerse. Los dos son None cuando no se pudo
    leer, y entonces no se comprueba ese lado en vez de suponer que falta todo.
    """
    raw = Path(raw) if raw else _ruta_raw()
    salida = {}
    if not raw.is_dir():
        return salida

    for ruta in sorted(raw.glob("doc_*.json")):
        if not exportador.es_ficha_mof(ruta.stem):
            continue
        uid = exportador.unidad_id_de_doc(ruta.stem)
        if uid is None:
            continue
        try:
            with open(ruta, encoding="utf-8") as f:
                bloque = (json.load(f) or {}).get("mof") or {}
        except Exception:
            bloque = {}
        salida[uid] = {
            "huella": bloque.get("hash_listado"),
            "indexado": (ruta.stem in indexados) if indexados is not None
                        else True,
            "en_replica": (uid in en_replica) if en_replica is not None
                          else True,
        }
    return salida

def _ruta_raw() -> Path:
    """Carpeta data/raw/. Se importa perezosamente para no arrastrar el modulo
    de ingesta cuando solo se usan las funciones puras de comparacion."""
    from .ingesta import RAW
    return RAW

def planificar(unidades_mof, local: dict, profundo: bool = False) -> Plan:
    """Funcion PURA: que unidades dar de alta, revisar o borrar.

    unidades_mof : lo que devolvio GET /api/v1/mof/unidades.
    local        : salida de estado_local().
    """
    ids_mof, huellas = [], {}
    for item in unidades_mof or []:
        try:
            uid = int((item or {}).get("id"))
        except (TypeError, ValueError):
            continue
        ids_mof.append(uid)
        huellas[uid] = huella_listado(item)

    en_mof = set(ids_mof)
    en_local = set(local)

    altas = sorted(en_mof - en_local)
    bajas = sorted(en_local - en_mof)

    cambios, iguales = [], 0
    for uid in sorted(en_mof & en_local):
        estado = local[uid] or {}
        if not estado.get("indexado"):
            cambios.append(uid)
        elif not estado.get("en_replica", True):
            cambios.append(uid)
        elif profundo:
            cambios.append(uid)
        elif estado.get("huella") != huellas[uid]:
            cambios.append(uid)
        else:
            iguales += 1

    plan = Plan(altas=altas, cambios=cambios, bajas=bajas,
                sin_cambios=iguales, profundo=bool(profundo), aviso=None)

    if bajas:
        proporcion = len(bajas) / max(1, len(en_local))
        if not en_mof:
            plan["aviso"] = (
                f"MOF no reporta NINGUNA unidad y en el indice hay "
                f"{len(en_local)}: eso no es un organigrama vacio, es una "
                f"fuente rota. No se aplican las bajas sin --forzar.")
        elif len(bajas) >= MINIMO_PARA_SOSPECHAR and proporcion >= UMBRAL_BAJAS:
            plan["aviso"] = (
                f"MOF reporta {len(en_mof)} unidades y en el indice hay "
                f"{len(en_local)}: se darian de baja {len(bajas)} "
                f"({proporcion:.0%}). No se aplican las bajas sin --forzar.")
    return plan

class Reconciliador:
    def __init__(self, cliente: ClienteMOF = None, ingestor=None,
                 facultad: str = None):
        self.cliente = cliente or ClienteMOF()
        self.ingestor = ingestor
        self.facultad = facultad or os.environ.get("MOF_FACULTAD") or None

    def _huellas(self, unidades):
        """unidad_id -> huella. Tolera ids ilegibles igual que planificar():
        una unidad rota no puede tumbar la reconciliacion de las demas."""
        salida = {}
        for u in unidades or []:
            try:
                salida[int((u or {}).get("id"))] = huella_listado(u)
            except (TypeError, ValueError):
                continue
        return salida

    def ejecutar(self, profundo: bool = False, dry_run: bool = False,
                 forzar: bool = False) -> dict:
        if not self.cliente.configurado:
            return {"ok": False, "motivo": "MOF_URL no configurado"}
        if self.ingestor is None or not self.ingestor.disponible:
            return {"ok": False, "motivo": "el motor todavia no esta cargado"}

        try:
            unidades = self.cliente.unidades()
        except ErrorMOF as exc:
            return {"ok": False, "motivo": f"MOF inalcanzable: {exc}"}

        self._refrescar_catalogo()

        ids_replica = getattr(self.ingestor, "ids_en_replica", None)
        local = estado_local(self.ingestor.raw,
                             self.ingestor.docs_indexados(),
                             ids_replica() if ids_replica else None)
        plan = planificar(unidades, local, profundo=profundo)
        huellas = self._huellas(unidades)

        resumen = {
            "ok": True,
            "plan": dict(plan),
            "dry_run": bool(dry_run),
            "aplicadas": 0,
            "reindexadas": 0,
            "bajas": 0,
            "errores": [],
        }
        if dry_run:
            return resumen

        for uid in plan["altas"] + plan["cambios"]:
            try:
                r = self._sincronizar(uid, huellas.get(uid))
            except (ErrorMOF, ValueError) as exc:
                resumen["errores"].append({"unidad": uid, "error": str(exc)})
                continue
            if r is None:
                continue
            resumen["aplicadas"] += 1
            if not r.get("sin_cambios"):
                resumen["reindexadas"] += 1

        if plan["bajas"]:
            if plan["aviso"] and not forzar:
                resumen["bajas_omitidas"] = plan["bajas"]
            else:
                for uid in plan["bajas"]:
                    try:
                        self.ingestor.aplicar("delete", unidad_id=uid)
                        resumen["bajas"] += 1
                    except ValueError as exc:
                        resumen["errores"].append(
                            {"unidad": uid, "error": str(exc)})

        if resumen["reindexadas"] or resumen["bajas"]:
            self.ingestor.persistir()
        return resumen

    def _refrescar_catalogo(self):
        """Trae el catalogo de relaciones a la replica.

        Le toca a la reconciliacion porque el empuje manda una unidad y nada
        mas: el catalogo no llega por ahi nunca. Es una peticion de tres filas
        y un fallo no puede frenar la reconciliacion —sin catalogo se agrupa
        igual, mostrando el codigo en vez de la etiqueta.
        """
        conexion = getattr(self.ingestor, "replica", None)
        if conexion is None:
            return
        try:
            from . import replica
            replica.guardar_catalogo_relaciones(
                conexion(), self.cliente.relaciones())
        except Exception:
            pass

    def _sincronizar(self, unidad_id: int, huella: str = None):
        """Baja la unidad completa y se la pasa al Ingestor.

        El Ingestor compara el texto real: si no cambio, no re-embebe nada y
        devuelve sin_cambios. La huella del listado se sella igual, que es lo
        que impide que la misma unidad se revise en cada corrida.
        """
        return self.aplicar_leida(unidad_id,
                                  self.cliente.unidad_completa(unidad_id),
                                  huella)

    def aplicar_leida(self, unidad_id: int, datos: dict, huella: str = None):
        """Aplica una unidad YA leida de MOF (salida de unidad_completa).

        Separado de la lectura para que quien sincroniza varias unidades pueda
        leerlas todas primero y decidir antes de tocar nada: la consulta de
        novedades frena ahi las bajas en masa que huelen a MOF roto.

        `huella` puede faltar cuando la unidad no vino del listado. Se calcula
        entonces sobre el detalle: la proyeccion normalizada da la MISMA huella
        desde los dos endpoints (test_la_misma_unidad_da_la_misma_huella_en_
        los_dos_endpoints), asi que la reconciliacion siguiente no la vuelve a
        revisar por una diferencia que no existe.
        """
        if not datos:
            return self.ingestor.aplicar("delete", unidad_id=unidad_id)
        return self.ingestor.aplicar(
            "upsert",
            unidad=datos["unidad"],
            funciones=datos["funciones"],
            personal=datos["personal"],
            padre=datos["padre"],
            facultad=self.facultad,
            marcas={"hash_listado": huella or huella_listado(datos["unidad"])},
        )

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Compara MOF con lo indexado y arregla la diferencia.")
    p.add_argument("--profundo", action="store_true",
                   help="Revisa cada unidad entera: unica forma de ver "
                        "cambios en funciones o personal. Cuesta ~3 "
                        "peticiones por unidad.")
    p.add_argument("--dry-run", action="store_true",
                   help="Dice que haria y no toca nada.")
    p.add_argument("--forzar", action="store_true",
                   help="Aplica las bajas aunque salte la valvula de "
                        "seguridad. Leer el aviso antes de usarlo.")
    args = p.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import motor_web
    from .ingesta import Ingestor

    motor = motor_web.MotorGaceta()
    if not motor.cargar():
        print(f"No se pudo cargar el motor: {motor.error}")
        return 2

    ingestor = Ingestor(motor=motor)
    try:
        resultado = Reconciliador(ingestor=ingestor).ejecutar(
            profundo=args.profundo, dry_run=args.dry_run, forzar=args.forzar)
        estado_replica = ingestor.estado_replica()
    finally:
        ingestor.cerrar()

    if not resultado.get("ok"):
        print(f"Reconciliacion no ejecutada: {resultado.get('motivo')}")
        return 1

    plan = resultado["plan"]
    print(f"Altas        : {len(plan['altas'])}")
    print(f"A revisar    : {len(plan['cambios'])}")
    print(f"Bajas        : {len(plan['bajas'])}")
    print(f"Sin cambios  : {plan['sin_cambios']}")
    r = estado_replica
    if r.get("ok"):
        print(f"Replica      : {r['unidades']} unidades, "
              f"{r['funciones']} funciones, {r['cargos']} cargos")
    else:
        print(f"Replica      : no disponible ({r.get('error')})")
    if plan.get("aviso"):
        print(f"\n  [AVISO] {plan['aviso']}")
    if not args.dry_run:
        print(f"\nAplicadas    : {resultado['aplicadas']} "
              f"({resultado['reindexadas']} reindexadas)")
        print(f"Bajas hechas : {resultado['bajas']}")
        if resultado.get("bajas_omitidas"):
            print(f"Bajas omitidas por la valvula: "
                  f"{len(resultado['bajas_omitidas'])}")
        for e in resultado["errores"]:
            print(f"  [ERROR] unidad {e['unidad']}: {e['error']}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
