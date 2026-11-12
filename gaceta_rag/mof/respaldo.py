# -*- coding: utf-8 -*-
"""
Llena la replica del organigrama desde un RESPALDO de la base de MOF.

Por que hace falta
──────────────────
La replica se llena hablando con la API de MOF: replica.sincronizar() para una
carga completa y novedades.Consultor para seguir los cambios. Eso exige que
MOF sea alcanzable desde donde corre la Gaceta. Cuando no lo es —MOF vive en
otra maquina, no hay cuenta todavia, o lo unico que hay es un pg_dump— la
replica se queda con lo que tenia, y una replica vieja NO SE NOTA: el chat
contesta "son 24 unidades" con la misma seguridad con la que contestaria bien.

Eso ya paso en esta maquina: la replica quedo con los datos del seeder de
pruebas de MOF (sigla igual al codigo en las 260 unidades, objetivos 'ABCD',
dos unidades 'COMISION 1' y 'COMSION 2') y el chat los daba como organigrama
de la universidad. Este modulo es el camino para reemplazarlos por los de MOF
cuando lo que hay es un respaldo.

Que es y que no es
──────────────────
No es una migracion del esquema de MOF ni un acceso a Postgres: lee el volcado
en texto plano (`pg_dump` sin -Fc) y REPRODUCE lo que la API de MOF habria
publicado de cada unidad, con las mismas formas que entiende el exportador. De
ahi en adelante todo sigue el camino normal (replica.guardar_unidad), asi que
la replica queda igual que si la hubiera llenado la sincronizacion.

Lo que eso implica, y esta probado en tests/test_mof_respaldo.py:

  - Las filas dadas de baja no entran. MOF borra con softRemove: la fila se
    queda con `deleted_at` y su API ya no la publica. En el respaldo del
    1/10/2026 habia tres unidades asi ('ejemplo' dos veces y 'zzzz').
  - Las etiquetas salen de los catalogos (tipo, nivel, tipo_unidad), no los
    ids; la relacion se guarda por su CODIGO ('L'), como la da la API.
  - Los escapes del formato COPY se deshacen: ese volcado trae 9 objetivos con
    saltos de linea y 98 funciones con tabuladores.
  - La fecha de las filas es la del RESPALDO, no la de la importacion: el chat
    cierra cada respuesta con "Datos de MOF al <fecha>" y esa fecha tiene que
    decir de cuando son los datos (ver vigencia.py).
  - El cursor de la auditoria queda donde termina el respaldo, asi que cuando
    MOF vuelva a ser alcanzable la consulta sigue desde ahi en vez de recargar
    el organigrama entero.

Los DOS destinos, como en la ingesta
────────────────────────────────────
Cada unidad vive en la Gaceta de dos formas (ver ingesta.py): la replica, que
permite contar y listar exacto, y la FICHA de texto indexada, que permite
encontrarla por significado ("quien se encarga de los titulos"). Sin --fichas
se llena solo la replica y no se carga ningun modelo; con --fichas cada unidad
pasa por el MISMO `ingesta.aplicar()` que un evento de MOF en vivo, asi que los
dos destinos se escriben juntos y una unidad dada de baja deja de encontrarse
tambien por significado.

Para las fichas hace falta embeber, pero NO el LLM: `MotorDeEmbeddings` carga
solo el modelo de embeddings y el indice FAISS, que es lo unico que el Ingestor
usa. Qwen y el reranker no se tocan.

Uso
───
    venv\\Scripts\\python.exe -m gaceta_rag.mof.respaldo respaldo.sql
    venv\\Scripts\\python.exe -m gaceta_rag.mof.respaldo respaldo.sql --fichas
    venv\\Scripts\\python.exe -m gaceta_rag.mof.respaldo respaldo.sql --ruta otra\\mof.sqlite

Hay que detener la API antes: la replica se reemplaza unidad por unidad y una
consulta en el medio veria el organigrama a medias (lo mismo que durante una
carga completa). Con --fichas, ademas, el indice se reescribe desde ESTE
proceso, y el motor de la API tiene el suyo en memoria: al persistir lo
pisaria.
"""
import os
import re
from datetime import datetime
from pathlib import Path

from . import exportador, ingesta, replica
from .novedades import CLAVE_CURSOR, _iso as _iso_utc, _parse as _parse_iso

TABLAS = ("unidad", "unidad_funcion", "cargo_unidad", "cargo",
          "catalogo_tipo", "catalogo_nivel", "catalogo_relacion",
          "tipo_unidad", "auditoria_cambio")

NULO = chr(92) + "N"

_FECHAS = ("created_at", "updated_at", "deleted_at")

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
            "v": "\v", chr(92): chr(92)}
_ESC = re.compile(re.escape(chr(92)) + r"(.)")

class ErrorRespaldo(RuntimeError):
    """El archivo no sirve como respaldo de MOF.

    Se lanza ANTES de tocar la replica: un volcado cortado, el volcado de otra
    base o una tabla de unidades vacia no pueden dejar la replica a medias ni
    vaciarla. Misma valvula que reconciliar.planificar y replica.sincronizar,
    y por la misma razon: una fuente que responde vacio esta rota mucho mas a
    menudo de lo que una institucion se queda sin unidades.
    """

def _desescapar(valor: str):
    """El valor de una celda COPY, o None si es NULL."""
    if valor == NULO:
        return None
    if chr(92) not in valor:
        return valor
    return _ESC.sub(lambda m: _ESCAPES.get(m.group(1), m.group(0)), valor)

def _bloque(texto: str, tabla: str):
    """Las filas de `COPY public.<tabla> (...) FROM stdin;`, como diccionarios.
    None si el volcado no trae esa tabla."""
    m = re.search(r"^COPY public\." + re.escape(tabla) +
                  r" \((.*?)\) FROM stdin;\n(.*?)^" + re.escape(chr(92)) + r"\.$",
                  texto, re.M | re.S)
    if m is None:
        return None
    columnas = [c.strip().strip('"') for c in m.group(1).split(",")]
    filas = []
    for linea in m.group(2).split("\n"):
        if linea == "":
            continue
        celdas = linea.split("\t")
        filas.append({c: _desescapar(v) for c, v in zip(columnas, celdas)})
    return filas

class Volcado:
    """Las tablas del respaldo que le importan a la replica."""

    def __init__(self, nombre: str, tablas: dict):
        self.nombre = nombre
        self.tablas = tablas

    def filas(self, tabla: str) -> list:
        return self.tablas.get(tabla) or []

    def vivas(self, tabla: str) -> list:
        """Las filas que la API de MOF publicaria: sin las dadas de baja."""
        return [f for f in self.filas(tabla) if not f.get("deleted_at")]

    def por_id(self, tabla: str, vivas: bool = True) -> dict:
        filas = self.vivas(tabla) if vivas else self.filas(tabla)
        return {f["id"]: f for f in filas if f.get("id")}

    @property
    def fecha(self):
        """De cuando son los datos: la marca de tiempo mas nueva del respaldo,
        en hora local y sin zona, como la escribe la replica.

        Se mira TODO (unidades, funciones, cargos, catalogos y la auditoria):
        un respaldo que solo cambio una funcion tiene que fechar por esa."""
        instantes = []
        for tabla in self.tablas:
            for f in self.filas(tabla):
                for columna in _FECHAS + ("created_at",):
                    dt = _parse_iso(f.get(columna)) if f.get(columna) else None
                    if dt:
                        instantes.append(dt)
        if not instantes:
            return None
        return (max(instantes).astimezone().replace(tzinfo=None)
                .isoformat(timespec="seconds"))

    @property
    def cursor(self):
        """Hasta donde llega la auditoria del respaldo, en el formato del
        cursor de novedades, o None si el respaldo no trae auditoria."""
        fechas = [f.get("created_at") for f in self.filas("auditoria_cambio")]
        validas = [dt for dt in (_parse_iso(f) for f in fechas if f) if dt]
        return _iso_utc(max(validas)) if validas else None

def leer(origen) -> Volcado:
    """Lee el volcado. ErrorRespaldo si no es uno de MOF."""
    ruta = Path(origen)
    if not ruta.exists():
        raise ErrorRespaldo(f"no existe el archivo {ruta}")
    texto = ruta.read_text(encoding="utf-8", errors="replace")
    tablas = {}
    for tabla in TABLAS:
        filas = _bloque(texto, tabla)
        if filas is not None:
            tablas[tabla] = filas
    if "unidad" not in tablas:
        raise ErrorRespaldo(
            f"{ruta.name} no parece un respaldo de MOF: no trae la tabla "
            f"'unidad' en formato COPY. Hace falta un pg_dump en texto plano "
            f"(sin -Fc).")
    return Volcado(ruta.name, tablas)

def _etiqueta(catalogo: dict, clave) -> str:
    """La descripcion de un catalogo, aunque el catalogo se haya dado de baja.

    A proposito se miran TAMBIEN los retirados: si una unidad viva todavia
    apunta a uno, vale mas la etiqueta que el vacio. Con tipo = '' la unidad
    desaparece de "que tipos de unidad hay" y sigue contando en el total, que
    es la peor de las dos respuestas.
    """
    fila = catalogo.get(clave)
    return exportador._txt((fila or {}).get("descripcion"))

def _funciones(volcado, unidad_id) -> list:
    filas = [f for f in volcado.vivas("unidad_funcion")
             if f.get("unidad_id") == unidad_id]
    filas.sort(key=lambda f: (_num(f.get("orden")) or 0, _num(f.get("id")) or 0))
    return [{"id": _num(f.get("id")), "orden": _num(f.get("orden")),
             "funcion": f.get("funcion"), "baseLegal": f.get("base_legal")}
            for f in filas]

def _cargos(volcado, unidad_id, cargos: dict) -> list:
    """Los cargos de la unidad, con el nombre que esta en la tabla `cargo`.

    cargo_unidad solo guarda ids. Un cargo inactivo o dado de baja no se
    publica: aparecer como cargo vigente de la unidad seria peor que faltar.
    """
    salida = []
    for f in volcado.vivas("cargo_unidad"):
        if f.get("unidad_id") != unidad_id or f.get("activo") == "f":
            continue
        cargo = cargos.get(f.get("cargo_id"))
        if not cargo:
            continue
        salida.append({"id": _num(f.get("id")), "cargoId": _num(f.get("cargo_id")),
                       "nombre": cargo.get("nombre"),
                       "descripcion": cargo.get("descripcion")})
    return salida

def unidades(volcado) -> list:
    """Cada unidad del respaldo con la forma del DETALLE de la API de MOF
    (mapDetail), mas sus funciones, sus cargos y su padre ya resuelto: lo mismo
    que devuelve cliente.unidad_completa()."""
    vivas = volcado.por_id("unidad")
    tipos = volcado.por_id("catalogo_tipo", vivas=False)
    niveles = volcado.por_id("catalogo_nivel", vivas=False)
    relaciones = volcado.por_id("catalogo_relacion", vivas=False)
    clases = volcado.por_id("tipo_unidad", vivas=False)
    cargos = volcado.por_id("cargo")

    salida = []
    for uid, f in sorted(vivas.items(), key=lambda kv: _num(kv[0]) or 0):
        unidad = {
            "id": _num(uid),
            "codigo": f.get("codigo"),
            "sigla": f.get("sigla"),
            "nombre": f.get("nombre"),
            "objetivo": f.get("objetivo"),
            "oficial": f.get("oficial") == "t",
            "tipo": _etiqueta(tipos, f.get("tipo_id")),
            "nivel": _etiqueta(niveles, f.get("nivel_id")),
            "relacion": exportador._txt(
                (relaciones.get(f.get("relacion_id")) or {}).get("codigo")),
            "clase": _etiqueta(clases, f.get("tipo_unidad_id")),
            "tipoUnidad": _num(f.get("tipo_unidad_id")),
            "resCreacion": f.get("res_creacion"),
            "baseLegal": f.get("base_legal"),
            "fecCreacion": f.get("fec_creacion"),
            "updatedAt": f.get("updated_at"),
        }
        padre = vivas.get(f.get("parent_id"))
        if padre:
            unidad["parent"] = {"id": _num(padre["id"]),
                                "codigo": padre.get("codigo"),
                                "nombre": padre.get("nombre")}
        salida.append({"unidad": unidad,
                       "funciones": _funciones(volcado, uid),
                       "personal": _cargos(volcado, uid, cargos),
                       "padre": None})
    return salida

def _num(valor):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None

def importar(origen, ruta=None, progreso=None, cursor: bool = True,
             ingestor=None) -> dict:
    """Reemplaza la replica por el organigrama del respaldo.

    origen  : el archivo .sql del pg_dump.
    ruta    : la replica a escribir (por defecto replica.RUTA_REPLICA).
    progreso: progreso(i, total), como en replica.sincronizar.
    cursor  : guardar el cursor de la auditoria del respaldo. Se apaga para un
              respaldo de OTRA instalacion de MOF, donde esos ids no dicen nada
              de la auditoria que la Gaceta va a consultar.
    ingestor: un ingesta.Ingestor para escribir TAMBIEN las fichas de texto del
              indice semantico. Sin el, solo se llena la replica y no se toca
              el indice ni se carga ningun modelo.

    Lo que el respaldo no trae, se va: una replica importada queda con el
    organigrama del respaldo y nada mas. Es el punto de todo esto. Con ingestor,
    eso incluye la ficha: una unidad dada de baja en MOF deja de encontrarse
    tambien por significado, no solo de contarse.
    """
    volcado = leer(origen)
    lote = unidades(volcado)
    if not lote:
        raise ErrorRespaldo(
            f"{volcado.nombre} no trae ninguna unidad viva "
            f"({len(volcado.filas('unidad'))} filas en la tabla 'unidad', "
            f"todas dadas de baja o ilegibles): no se toca la replica.")

    if ingestor is not None:
        if ruta is not None and Path(ruta).resolve() != Path(ingestor.ruta_replica).resolve():
            raise ErrorRespaldo(
                f"la replica pedida ({ruta}) no es la del ingestor "
                f"({ingestor.ruta_replica}): las fichas y la replica tienen "
                f"que ir al mismo lado.")
        ruta = ingestor.ruta_replica

    facultad = os.environ.get("MOF_FACULTAD") or None
    conn = replica.abrir(ruta)
    try:
        replica.guardar_catalogo_relaciones(
            conn, [{"codigo": f.get("codigo"), "descripcion": f.get("descripcion")}
                   for f in volcado.vivas("catalogo_relacion")])

        previas = {f[0] for f in conn.execute("SELECT id FROM unidad")}
        vistas, errores = set(), []
        fichas = chunks = 0
        for i, datos in enumerate(lote, 1):
            uid = datos["unidad"]["id"]
            try:
                if ingestor is None:
                    replica.guardar_unidad(conn, datos["unidad"], datos["funciones"],
                                           datos["personal"], datos["padre"])
                else:
                    r = ingestor.aplicar("upsert", facultad=facultad, **datos)
                    fichas += 1
                    chunks += int(r.get("chunks") or 0)
                vistas.add(uid)
            except Exception as exc:
                errores.append({"unidad": uid, "error": str(exc)})
            if progreso:
                progreso(i, len(lote))

        borradas = 0
        for uid in sorted(previas - vistas):
            if ingestor is None:
                replica.borrar_unidad(conn, uid)
            else:
                ingestor.aplicar("delete", unidad_id=uid)
            borradas += 1

        fecha = volcado.fecha or datetime.now().isoformat(timespec="seconds")
        conn.execute("UPDATE unidad SET actualizado = ?", (fecha,))
        _meta(conn, "origen", f"respaldo:{volcado.nombre}")
        _meta(conn, "origen_fecha", fecha)
        if cursor:
            _meta(conn, CLAVE_CURSOR, volcado.cursor)
        conn.commit()

        persistido = False
        if ingestor is not None:
            persistido = bool(ingestor.persistir().get("persistido"))

        return dict(replica.resumen(conn), ok=True, ruta=str(ruta or replica.RUTA_REPLICA),
                    origen=volcado.nombre, fecha=fecha, cursor=volcado.cursor if cursor else None,
                    en_respaldo=len(lote), importadas=len(vistas),
                    borradas=borradas, sin_padre=sum(
                        1 for d in lote if "parent" not in d["unidad"]),
                    bajas_en_mof=len(volcado.filas("unidad")) - len(lote),
                    fichas=fichas, chunks=chunks, persistido=persistido,
                    errores=errores)
    finally:
        conn.close()

class MotorDeEmbeddings:
    """Lo MINIMO que el Ingestor necesita de un motor para escribir fichas: el
    modelo de embeddings y el indice FAISS con su metadata.

    No carga el LLM de 7B ni el reranker. Embeber 260 fichas no necesita ni
    redactar ni reordenar nada, y cargar Qwen aqui ocuparia la GPU para nada;
    `ingesta._embeber` y `persistir` solo usan estos tres atributos (mas
    `requiere_e5`, que decide el prefijo "passage: " del modelo e5).

    Las rutas se derivan de la base de chunks, como en el Ingestor: asi un
    banco de pruebas que redirige la base se lleva tambien el indice.
    """

    def __init__(self, ruta_sqlite=None):
        import json

        for clave, valor in (("HF_HUB_DISABLE_XET", "1"), ("HF_HUB_OFFLINE", "1"),
                             ("TRANSFORMERS_OFFLINE", "1"),
                             ("TOKENIZERS_PARALLELISM", "false")):
            os.environ.setdefault(clave, valor)

        import faiss
        from sentence_transformers import SentenceTransformer

        junto = Path(ruta_sqlite or ingesta.RUTA_SQLITE).parent
        with open(junto / "embedding_config_v2.json", encoding="utf-8") as f:
            config = json.load(f)
        modelo_id = config["modelo_embeddings"]
        self.requiere_e5 = config.get("requiere_prefijo_e5",
                                      "e5" in modelo_id.lower())
        self.modelo_emb = SentenceTransformer(modelo_id)
        self.faiss_index = faiss.read_index(str(junto / ingesta.RUTA_FAISS.name))
        with open(junto / ingesta.RUTA_META.name, encoding="utf-8") as f:
            self.faiss_meta = json.load(f)

def ingestor_de_fichas(ruta_sqlite=None, ruta_replica=None):
    """Un Ingestor listo para escribir fichas, con un motor de solo embeddings.
    Hay que cerrarlo al terminar (deja la conexion de escritura abierta)."""
    return ingesta.Ingestor(motor=MotorDeEmbeddings(ruta_sqlite),
                            ruta_sqlite=ruta_sqlite, ruta_replica=ruta_replica)

def _meta(conn, clave: str, valor) -> None:
    if valor is None:
        conn.execute("DELETE FROM replica_meta WHERE clave = ?", (clave,))
    else:
        conn.execute("INSERT OR REPLACE INTO replica_meta VALUES (?,?)",
                     (clave, str(valor)))

def main(argv=None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        description="Llena la replica del organigrama desde un respaldo "
                    "(pg_dump en texto plano) de la base de MOF. Sin --fichas "
                    "no necesita el motor ni el indice.",
        epilog="Hay que detener la API antes: la replica se reemplaza unidad "
               "por unidad y, con --fichas, el indice se reescribe desde ESTE "
               "proceso (el motor de la API tiene el suyo en memoria y lo "
               "pisaria al persistir). Guardar una copia de la replica "
               "anterior si interesa poder volver.")
    p.add_argument("respaldo", help="archivo .sql del pg_dump de MOF")
    p.add_argument("--ruta", default=None,
                   help=f"replica a escribir (por defecto {replica.RUTA_REPLICA})")
    p.add_argument("--sin-cursor", action="store_true",
                   help="no guardar el cursor de la auditoria (respaldo de "
                        "otra instalacion de MOF)")
    p.add_argument("--fichas", action="store_true",
                   help="escribir TAMBIEN las fichas de texto del indice "
                        "semantico (carga el modelo de embeddings, no el LLM)")
    p.add_argument("--base", default=None,
                   help="base de chunks junto a la que viven indice y replica "
                        f"(por defecto {ingesta.RUTA_SQLITE})")
    args = p.parse_args(argv)

    def progreso(i, total):
        if i % 25 == 0 or i == total:
            print(f"  {i}/{total}", flush=True)

    ingestor = None
    if args.fichas:
        print("Cargando el modelo de embeddings y el indice "
              "(sin LLM ni reranker)...")
        try:
            ingestor = ingestor_de_fichas(args.base, args.ruta)
        except Exception as exc:
            print(f"No se pudo preparar el indice: "
                  f"{exc.__class__.__name__}: {exc}")
            return 1
        print(f"  Vectores en el indice: {ingestor.motor.faiss_index.ntotal}")
        print("  El indice se baja a disco varias veces durante la carga "
              f"(cada {ingesta.FLUSH_CADA} unidades): si se corta, lo "
              "persistido queda y el resto se repara al repetir.")

    print(f"Leyendo {args.respaldo}...")
    try:
        r = importar(args.respaldo, ruta=args.ruta, progreso=progreso,
                     cursor=not args.sin_cursor, ingestor=ingestor)
    except ErrorRespaldo as exc:
        print(f"No se pudo: {exc}")
        return 1
    finally:
        if ingestor is not None:
            ingestor.cerrar()

    print(f"\nReplica       : {r['ruta']}")
    print(f"En el respaldo: {r['en_respaldo']} unidades vivas "
          f"({r['bajas_en_mof']} dadas de baja en MOF, no entran)")
    print(f"Importadas    : {r['importadas']}")
    print(f"Borradas      : {r['borradas']} (estaban en la replica y no en el respaldo)")
    print(f"Replica       : {r['unidades']} unidades, {r['funciones']} funciones, "
          f"{r['cargos']} cargos, {r['raices']} raiz/raices")
    if r["fichas"]:
        print(f"Fichas        : {r['fichas']} indexadas, {r['chunks']} chunks"
              + ("" if r["persistido"] else " (NADA bajado a disco: revisar)"))
    print(f"Datos de MOF al: {r['fecha']}")
    sin_cursor = "(sin cursor: la proxima consulta a MOF recarga todo)"
    print(f"Cursor auditoria: {r['cursor'] or sin_cursor}")
    if r["huerfanas"]:
        print(f"  [AVISO] {r['huerfanas']} unidades apuntan a un padre que no "
              f"esta en la replica")
    if r["sin_padre"] > 1:
        print(f"  [AVISO] {r['sin_padre']} unidades quedaron sin padre (su "
              f"padre esta dado de baja en MOF)")
    for e in r["errores"]:
        print(f"  [ERROR] unidad {e['unidad']}: {e['error']}")
    return 0 if not r["errores"] else 1

if __name__ == "__main__":
    raise SystemExit(main())
