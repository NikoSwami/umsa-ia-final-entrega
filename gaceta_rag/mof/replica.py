# -*- coding: utf-8 -*-
"""
Replica local y ESTRUCTURADA del organigrama de MOF.

Que resuelve que no resolvia lo anterior
────────────────────────────────────────
El exportador convierte cada unidad en una FICHA de texto que se indexa como
un documento mas. Eso hace que el chat pueda conversar sobre el organigrama
—"de que se encarga el IIQ", "quien resguarda las instalaciones"— porque busca
por significado.

Lo que no puede hacer con documentos es CONTAR ni LISTAR de forma exacta.
"Cuantas unidades dependen de Secretaria General" o "damelas todas" no se
responden buscando parecidos: se responden con una consulta. Para eso hace
falta una tabla, y esto es esa tabla.

Las dos cosas conviven y no compiten: la ficha sirve para encontrar, la replica
para contar. Comparten el mismo flujo de datos, asi que un cambio en MOF
actualiza las dos en el mismo evento.

Que NO es
─────────
No es una copia de la base de MOF. Es una copia de lo que la API de MOF
publica, con los campos que el chat necesita. MOF sigue siendo el dueño del
dato: aqui nunca se escribe nada que no haya venido de el, y no hay forma de
que esta replica le devuelva un cambio.

Contrato seguro, como el resto de los canales opcionales: si el archivo no
existe, todo lo demas funciona igual y las consultas devuelven vacio.
"""
import os
import sqlite3
from pathlib import Path

from . import exportador

DATA = Path(__file__).resolve().parents[1] / "data"
RUTA_REPLICA = Path(os.environ.get("MOF_REPLICA") or (DATA / "mof.sqlite"))

ESQUEMA = 5

_DDL = """
CREATE TABLE unidad (
    id            INTEGER PRIMARY KEY,
    codigo        TEXT,
    sigla         TEXT,
    nombre        TEXT NOT NULL,
    objetivo      TEXT,
    padre_id      INTEGER,
    padre_nombre  TEXT,
    tipo          TEXT,
    nivel         TEXT,
    relacion      TEXT,
    clase         TEXT,
    clase_id      TEXT,
    oficial       INTEGER NOT NULL DEFAULT 0,
    res_creacion  TEXT,
    base_legal    TEXT,
    fec_creacion  TEXT,
    -- Nombre y sigla en minusculas y sin acentos. Existe porque SQLite no
    -- pliega acentos: buscar "quimica" no encontraba "QUIMICA" con tilde, que
    -- es justo como la gente la escribe.
    busqueda      TEXT NOT NULL DEFAULT '',
    actualizado   TEXT NOT NULL
);
CREATE INDEX idx_unidad_padre  ON unidad(padre_id);
CREATE INDEX idx_unidad_sigla  ON unidad(sigla);
CREATE INDEX idx_unidad_tipo   ON unidad(tipo);
CREATE INDEX idx_unidad_rel    ON unidad(relacion);
CREATE INDEX idx_unidad_busq   ON unidad(busqueda);

CREATE TABLE unidad_funcion (
    id          INTEGER PRIMARY KEY,
    unidad_id   INTEGER NOT NULL,
    orden       INTEGER,
    funcion     TEXT NOT NULL,
    base_legal  TEXT
);
CREATE INDEX idx_funcion_unidad ON unidad_funcion(unidad_id);

CREATE TABLE cargo_unidad (
    id          INTEGER PRIMARY KEY,
    unidad_id   INTEGER NOT NULL,
    cargo_id    INTEGER,
    nombre      TEXT NOT NULL,
    descripcion TEXT
);
CREATE INDEX idx_cargo_unidad ON cargo_unidad(unidad_id);

CREATE TABLE catalogo_relacion (
    codigo      TEXT PRIMARY KEY,
    descripcion TEXT NOT NULL
);

CREATE TABLE replica_meta (
    clave TEXT PRIMARY KEY,
    valor TEXT
);
"""

def abrir(ruta=None, crear: bool = True) -> sqlite3.Connection:
    """Conexion a la replica. Crea el esquema si hace falta."""
    ruta = Path(ruta) if ruta else RUTA_REPLICA
    if not crear and not ruta.exists():
        raise FileNotFoundError(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(ruta), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    _asegurar_esquema(conn)
    return conn

def abrir_lectura(ruta=None) -> sqlite3.Connection:
    """Conexion de SOLO LECTURA, propia de quien la pide.

    Para los endpoints. Compartir la conexion de escritura de la ingesta con
    un hilo que atiende peticiones hacia que una consulta pudiera leer una
    unidad a medio reemplazar (sin sus funciones, por ejemplo). Con WAL, un
    lector aparte ve siempre el ultimo estado confirmado y no frena a nadie.

    No crea nada: si la replica todavia no existe, FileNotFoundError.
    """
    from urllib.request import pathname2url

    ruta = Path(ruta) if ruta else RUTA_REPLICA
    if not ruta.exists():
        raise FileNotFoundError(ruta)
    conn = sqlite3.connect(f"file:{pathname2url(str(ruta.resolve()))}?mode=ro",
                           uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def _asegurar_esquema(conn: sqlite3.Connection) -> None:
    version = _version(conn)
    if version == ESQUEMA:
        return
    for tabla in ("unidad", "unidad_funcion", "cargo_unidad",
                  "catalogo_relacion", "replica_meta"):
        conn.execute(f"DROP TABLE IF EXISTS {tabla}")
    conn.executescript(_DDL)
    conn.execute("INSERT INTO replica_meta VALUES ('esquema', ?)", (str(ESQUEMA),))
    conn.commit()

def _version(conn: sqlite3.Connection) -> int:
    try:
        fila = conn.execute(
            "SELECT valor FROM replica_meta WHERE clave = 'esquema'").fetchone()
        return int(fila[0]) if fila else 0
    except sqlite3.DatabaseError:
        return 0

def disponible(ruta=None) -> bool:
    """True si la replica existe y tiene unidades. Sin esto, no-op."""
    ruta = Path(ruta) if ruta else RUTA_REPLICA
    if not ruta.exists():
        return False
    try:
        conn = sqlite3.connect(str(ruta))
        n = conn.execute("SELECT COUNT(*) FROM unidad").fetchone()[0]
        conn.close()
        return n > 0
    except sqlite3.DatabaseError:
        return False

def guardar_unidad(conn, unidad: dict, funciones=None, personal=None,
                   padre: dict = None) -> int:
    """Inserta o reemplaza una unidad con sus funciones y cargos.

    Los campos se leen con los mismos accesores que usa el exportador, que
    entienden las DOS formas en que MOF describe una unidad (el listado y el
    detalle traen otros nombres y otros valores). Sin eso, la replica quedaria
    con 'L' o con 'Lineal' segun de donde vino el dato.
    """
    uid = int((unidad or {})["id"])
    try:
        _escribir(conn, uid, unidad, funciones, personal, padre)
        conn.commit()
    except Exception:
        conn.rollback()
        try:
            borrar_unidad(conn, uid)
        except Exception:
            pass
        raise
    return uid

def _escribir(conn, uid, unidad, funciones, personal, padre):
    from datetime import datetime

    unidad = unidad or {}
    _t = exportador._txt

    previo = conn.execute(
        "SELECT clase FROM unidad WHERE id = ?", (uid,)).fetchone()
    clase_txt = exportador._clase_texto(unidad) or (previo["clase"]
                                                    if previo else "")

    conn.execute("""
        INSERT OR REPLACE INTO unidad
          (id, codigo, sigla, nombre, objetivo, padre_id, padre_nombre,
           tipo, nivel, relacion, clase, clase_id, oficial,
           res_creacion, base_legal, fec_creacion, busqueda, actualizado)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        uid,
        _t(unidad.get("codigo")),
        _t(unidad.get("sigla")),
        _t(unidad.get("nombre")) or f"Unidad {uid}",
        _t(unidad.get("objetivo")),
        int(exportador._padre_id(unidad, padre) or 0) or None,
        exportador._nombre_padre(unidad, padre),
        exportador._cat(unidad, "tipo"),
        exportador._cat(unidad, "nivel"),
        exportador._relacion_codigo(unidad),
        clase_txt,
        exportador._clase_id(unidad),
        1 if unidad.get("oficial") else 0,
        _t(unidad.get("resCreacion") or unidad.get("res_creacion")),
        _t(unidad.get("baseLegal") or unidad.get("base_legal")),
        _t(unidad.get("fecCreacion") or unidad.get("fec_creacion"))[:10],
        exportador.normalizar(f"{_t(unidad.get('nombre'))} "
                              f"{_t(unidad.get('sigla'))}"),
        datetime.now().isoformat(timespec="seconds"),
    ))

    conn.execute("DELETE FROM unidad_funcion WHERE unidad_id = ?", (uid,))
    for i, f in enumerate(funciones or [], 1):
        f = f or {}
        texto = exportador._ORDINAL.sub("", _t(f.get("funcion")))
        if not texto:
            continue
        conn.execute(
            "INSERT INTO unidad_funcion (id, unidad_id, orden, funcion, base_legal)"
            " VALUES (?,?,?,?,?)",
            (_id_o(f.get("id"), uid * 1000 + i), uid,
             f.get("orden") if isinstance(f.get("orden"), int) else i,
             texto, _t(f.get("baseLegal") or f.get("base_legal"))))

    conn.execute("DELETE FROM cargo_unidad WHERE unidad_id = ?", (uid,))
    for i, c in enumerate(personal or [], 1):
        c = c or {}
        nombre = _t(c.get("nombre")) or _t(c.get("cargo_nombre"))
        if not nombre:
            continue
        conn.execute(
            "INSERT INTO cargo_unidad (id, unidad_id, cargo_id, nombre, descripcion)"
            " VALUES (?,?,?,?,?)",
            (_id_o(c.get("id"), uid * 1000 + i), uid,
             _entero(c.get("cargoId")), nombre, _t(c.get("descripcion"))))

def _id_o(valor, alternativo: int) -> int:
    try:
        return int(valor)
    except (TypeError, ValueError):
        return alternativo

def _entero(valor):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None

def borrar_unidad(conn, unidad_id) -> bool:
    """Saca una unidad y todo lo suyo. True si existia."""
    uid = int(unidad_id)
    existia = conn.execute(
        "SELECT 1 FROM unidad WHERE id = ?", (uid,)).fetchone() is not None
    conn.execute("DELETE FROM unidad_funcion WHERE unidad_id = ?", (uid,))
    conn.execute("DELETE FROM cargo_unidad WHERE unidad_id = ?", (uid,))
    conn.execute("DELETE FROM unidad WHERE id = ?", (uid,))
    conn.commit()
    return existia

def contar_unidades(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM unidad").fetchone()[0]

def unidad(conn, unidad_id):
    return conn.execute("SELECT * FROM unidad WHERE id = ?",
                        (int(unidad_id),)).fetchone()

def por_sigla(conn, sigla):
    """Sigla exacta, sin distinguir acentos ni mayusculas."""
    return conn.execute(
        "SELECT * FROM unidad WHERE busqueda LIKE ? ORDER BY LENGTH(nombre)",
        (f"% {exportador.normalizar(sigla)}",)).fetchone()

def hijas_de(conn, unidad_id) -> list:
    """Unidades que dependen DIRECTAMENTE de una."""
    return conn.execute(
        "SELECT * FROM unidad WHERE padre_id = ? ORDER BY codigo, nombre",
        (int(unidad_id),)).fetchall()

def descendientes_de(conn, unidad_id) -> list:
    """Toda la rama debajo de una unidad, a cualquier profundidad."""
    return conn.execute("""
        WITH RECURSIVE rama(id) AS (
            SELECT id FROM unidad WHERE padre_id = ?
            UNION
            SELECT u.id FROM unidad u JOIN rama r ON u.padre_id = r.id
        )
        SELECT u.* FROM unidad u JOIN rama r ON u.id = r.id
        ORDER BY u.codigo, u.nombre
    """, (int(unidad_id),)).fetchall()

def raices(conn) -> list:
    """Unidades sin padre: la cima del organigrama."""
    return conn.execute(
        "SELECT * FROM unidad WHERE padre_id IS NULL ORDER BY codigo, nombre"
    ).fetchall()

def todas(conn) -> list:
    """Todo el organigrama, por nombre."""
    return conn.execute("SELECT * FROM unidad ORDER BY nombre").fetchall()

def por_tipo(conn, tipo) -> list:
    return conn.execute(
        "SELECT * FROM unidad WHERE tipo = ? COLLATE NOCASE ORDER BY nombre",
        (str(tipo).strip(),)).fetchall()

def por_relacion(conn, relacion) -> list:
    """Unidades con esa relacion, por su CODIGO ('L', 'S', 'F')."""
    return conn.execute(
        "SELECT * FROM unidad WHERE relacion = ? ORDER BY nombre",
        (str(relacion).strip(),)).fetchall()

def tipos(conn) -> list:
    """[(tipo, cuantas)] — para responder 'que tipos de unidad hay'."""
    return conn.execute("""
        SELECT tipo, COUNT(*) AS n FROM unidad
        WHERE tipo <> '' GROUP BY tipo ORDER BY n DESC, tipo
    """).fetchall()

def guardar_catalogo_relaciones(conn, filas) -> int:
    """Guarda el catalogo de relaciones. Sin el, agrupar por relacion diria
    'L' en vez de 'Lineal'."""
    n = 0
    for f in filas or []:
        codigo = exportador._txt((f or {}).get("codigo"))
        desc = exportador._txt((f or {}).get("descripcion")
                               or (f or {}).get("nombre"))
        if not codigo or not desc:
            continue
        conn.execute("INSERT OR REPLACE INTO catalogo_relacion VALUES (?,?)",
                     (codigo, desc))
        n += 1
    conn.commit()
    return n

def relaciones(conn) -> list:
    """[(codigo, etiqueta, cuantas)]. Se agrupa por el CODIGO, que es lo unico
    que los dos endpoints de MOF dan igual; la etiqueta sale del catalogo, y
    si todavia no se trajo, se muestra el codigo en vez de nada."""
    return conn.execute("""
        SELECT u.relacion AS relacion,
               COALESCE(c.descripcion, u.relacion) AS etiqueta,
               COUNT(*) AS n
        FROM unidad u
        LEFT JOIN catalogo_relacion c ON c.codigo = u.relacion
        WHERE u.relacion <> ''
        GROUP BY u.relacion, etiqueta ORDER BY n DESC, u.relacion
    """).fetchall()

def funciones_de(conn, unidad_id) -> list:
    return conn.execute(
        "SELECT * FROM unidad_funcion WHERE unidad_id = ? ORDER BY orden, id",
        (int(unidad_id),)).fetchall()

def cargos_de(conn, unidad_id) -> list:
    return conn.execute(
        "SELECT * FROM cargo_unidad WHERE unidad_id = ? ORDER BY nombre",
        (int(unidad_id),)).fetchall()

def buscar(conn, texto, limite: int = 20) -> list:
    """Coincidencia por nombre o sigla, ignorando acentos y mayusculas.

    No reemplaza a la busqueda semantica: esto es para resolver un nombre, no
    para entender una pregunta. Pero tiene que encontrar "quimica" escrito sin
    tilde, porque asi es como se escribe."""
    patron = f"%{exportador.normalizar(texto)}%"
    return conn.execute("""
        SELECT * FROM unidad WHERE busqueda LIKE ?
        ORDER BY LENGTH(nombre) LIMIT ?
    """, (patron, int(limite))).fetchall()

def resumen(conn) -> dict:
    """Numeros de control, para saber si la replica esta sana."""
    q = lambda s: conn.execute(s).fetchone()[0]
    return {
        "unidades": q("SELECT COUNT(*) FROM unidad"),
        "funciones": q("SELECT COUNT(*) FROM unidad_funcion"),
        "cargos": q("SELECT COUNT(*) FROM cargo_unidad"),
        "raices": q("SELECT COUNT(*) FROM unidad WHERE padre_id IS NULL"),
        "catalogo_relaciones": q("SELECT COUNT(*) FROM catalogo_relacion"),
        "huerfanas": q("""SELECT COUNT(*) FROM unidad u
                          WHERE u.padre_id IS NOT NULL AND NOT EXISTS
                          (SELECT 1 FROM unidad p WHERE p.id = u.padre_id)"""),
    }

def sincronizar(cliente=None, ruta=None, progreso=None) -> dict:
    """Trae TODO el organigrama de MOF y lo deja en la replica.

    Las unidades que ya no estan en MOF se borran aqui tambien, con la misma
    valvula que la reconciliacion: si MOF devuelve una lista vacia no se
    interpreta como "borra todo", porque una fuente que responde vacio esta
    rota mucho mas a menudo de lo que una institucion se queda sin unidades.
    """
    from .cliente import ClienteMOF

    cliente = cliente or ClienteMOF()
    if not cliente.configurado:
        return {"ok": False, "motivo": "MOF_URL no configurado"}

    try:
        listado = cliente.unidades()
    except Exception as exc:
        return {"ok": False, "motivo": f"MOF inalcanzable: {exc}"}

    conn = abrir(ruta)
    try:
        guardar_catalogo_relaciones(conn, cliente.relaciones())
    except Exception:
        pass
    previas = {fila[0] for fila in conn.execute("SELECT id FROM unidad")}
    vistas, errores = set(), []

    for i, item in enumerate(listado or [], 1):
        try:
            uid = int((item or {}).get("id"))
        except (TypeError, ValueError):
            continue
        try:
            datos = cliente.unidad_completa(uid)
            if not datos:
                continue
            guardar_unidad(conn, datos["unidad"], datos["funciones"],
                           datos["personal"], datos["padre"])
            vistas.add(uid)
        except Exception as exc:
            errores.append({"unidad": uid, "error": str(exc)})
        if progreso:
            progreso(i, len(listado))

    sobrantes = sorted(previas - vistas)
    borradas, omitidas = 0, []
    if sobrantes:
        if not vistas and previas:
            omitidas = sobrantes
        else:
            for uid in sobrantes:
                borrar_unidad(conn, uid)
                borradas += 1

    salida = dict(resumen(conn), ok=True, en_mof=len(listado or []),
                  sincronizadas=len(vistas), borradas=borradas,
                  errores=errores)
    if omitidas:
        salida["borrados_omitidos"] = omitidas
        salida["aviso"] = (
            f"MOF no devolvio ninguna unidad utilizable y en la replica hay "
            f"{len(previas)}: no se borro nada. Revisar la conexion.")
    conn.close()
    return salida

def main(argv=None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        description="Trae el organigrama de MOF a la replica estructurada "
                    "local. No necesita el motor ni el indice.")
    p.add_argument("--estado", action="store_true",
                   help="Solo informa que hay en la replica y sale.")
    p.add_argument("--ruta", default=None,
                   help=f"Archivo de la replica (por defecto {RUTA_REPLICA}).")
    args = p.parse_args(argv)

    if args.estado:
        conn = abrir(args.ruta)
        r = resumen(conn)
        print(f"Replica   : {args.ruta or RUTA_REPLICA}")
        print(f"Unidades  : {r['unidades']}")
        print(f"Funciones : {r['funciones']}")
        print(f"Cargos    : {r['cargos']}")
        print(f"Raices    : {r['raices']}")
        if r["huerfanas"]:
            print(f"  [AVISO] {r['huerfanas']} unidades apuntan a un padre "
                  f"que no esta en la replica")
        conn.close()
        return 0

    def progreso(i, total):
        if i % 20 == 0 or i == total:
            print(f"  {i}/{total}", flush=True)

    print("Trayendo el organigrama de MOF...")
    r = sincronizar(ruta=args.ruta, progreso=progreso)
    if not r.get("ok"):
        print(f"No se pudo: {r.get('motivo')}")
        return 1

    print(f"\nEn MOF        : {r['en_mof']}")
    print(f"Sincronizadas : {r['sincronizadas']}")
    print(f"Borradas      : {r['borradas']}")
    print(f"Replica       : {r['unidades']} unidades, {r['funciones']} "
          f"funciones, {r['cargos']} cargos")
    if r.get("aviso"):
        print(f"\n  [AVISO] {r['aviso']}")
    for e in r["errores"]:
        print(f"  [ERROR] unidad {e['unidad']}: {e['error']}")
    return 0 if not r["errores"] else 1

if __name__ == "__main__":
    raise SystemExit(main())
