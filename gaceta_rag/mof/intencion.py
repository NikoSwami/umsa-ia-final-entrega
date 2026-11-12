# -*- coding: utf-8 -*-
"""
Que pregunta una consulta sobre el ORGANIGRAMA de MOF. Reglas fijas, sin LLM.

Para que existe
───────────────
"dame todas las unidades de tipo asesoramiento" no tiene signos de pregunta
ni palabras del dominio de la Gaceta: motor_qwen.clasificar_intencion la marca
"sin_sentido" y el chat contesta "No entendí" sin buscar nada. Esto la
reconoce leyendo la replica: los nombres, tipos y relaciones que MOF publica.

Dos exigencias que tiran para lados distintos
─────────────────────────────────────────────
  - que la pregunta del organigrama llegue;
  - que ninguna pregunta de RESOLUCIONES se desvie. Nombrar una unidad no
    alcanza: "¿sigue vigente la resolucion sobre el nombramiento de
    responsable del instituto de investigacion matematica?" nombra una que
    existe y es una pregunta de resoluciones. Hace falta una INTENCION de
    organigrama: contar o listar unidades, de quien depende una, sus
    funciones, sus cargos, su objetivo, que resolucion la creo.

Unidades: nunca una inventada
─────────────────────────────
La mencion empieza donde el usuario empieza a nombrar una unidad: en una
palabra de tipo de unidad (departamento, division, instituto...), sacadas de
los nombres reales de la replica. Se busca con replica.buscar y se exige que
las palabras de la mencion esten SEGUIDAS dentro del nombre, sin importar
acentos, mayusculas ni puntuacion. Si ninguna unidad las contiene, la unidad
es None aunque haya algo parecido: "Instituto de Quimica" no es el "Instituto
de Investigaciones Quimicas". Si empatan varias (dos unidades homonimas),
tambien None, con los candidatos: elegir una al azar es inventar.

Para las preguntas SOBRE una unidad (jerarquia, describir, funciones, cargos,
creacion) la mencion tiene que estar pegada a la palabra que decide la
intencion, con nada mas que articulos y verbos de relleno en el medio: en
"¿cual es el objetivo del reglamento de la Facultad de Ingenieria?" el
objetivo es del reglamento, no de la facultad.

Contrato seguro, como el resto de MOF: sin replica o con la replica vacia,
detectar devuelve None y el chat se comporta exactamente como antes.
"""
import re
import sqlite3
from bisect import bisect_right
from dataclasses import dataclass

from . import exportador, replica

TIPOS = ("contar", "listar", "jerarquia", "describir", "funciones", "cargos",
         "creacion")
SOBRE_UNA_UNIDAD = frozenset({"jerarquia", "describir", "funciones", "cargos",
                              "creacion"})

@dataclass(frozen=True)
class Filtro:
    campo: str
    valor: str
    etiqueta: str

@dataclass(frozen=True)
class Intencion:
    tipo: str
    unidad_id: int = None
    unidad_nombre: str = None
    mencion: str = None
    candidatos: tuple = ()
    filtro: Filtro = None
    alcance: str = None
    agrupar: str = None

_CONECTORES = frozenset("de del la las el los lo y e en a al o u".split())

_CORTES = frozenset("""hay tiene tienen depende dependen cumple cumplen es son
    esta estan fue fueron que cual cuales cuantas cuantos quien quienes segun
    contando tipo tipos cada como donde cuando para por con sus su""".split())

_PEGAMENTO = _CONECTORES | frozenset("""tiene tienen cumple cumplen realiza
    realizan desempena desempenan hay existen son es esta estan que se sus su
    principales actual actuales asignadas asignados segun mof manual""".split())

_DOCUMENTOS = frozenset("""resolucion resoluciones reglamento reglamentos norma
    normas convocatoria convocatorias documento documentos decreto ley estatuto
    acta informe proyecto gaceta""".split())
_ACTOS = frozenset("""designacion designaciones designar designo designaron
    designado designada nombramiento nombramientos nombrar nombro convoco
    convocaron convocar convocado eleccion elecciones elegir posesion renuncia
    contratacion contratar""".split())

_CABEZAS_FIJAS = frozenset("""facultad departamento division seccion direccion
    instituto secretaria vicerrectorado rectorado vicedecanato decanato centro
    unidad posgrado postgrado carrera jefatura oficina biblioteca television
    defensoria""".split())

@dataclass(frozen=True)
class _Token:
    norma: str
    ini: int
    fin: int

def _tokens(texto) -> list:
    return re.findall(r"[a-z0-9]+", exportador.normalizar(texto))

def _contenido(tokens) -> list:
    return [t for t in tokens if t not in _CONECTORES]

def _tokens_con_posicion(consulta) -> list:
    salida = []
    for m in re.finditer(r"[^\W_]+", consulta):
        for norma in _tokens(m.group()):
            salida.append(_Token(norma, m.start(), m.end()))
    return salida

def _frases(consulta, tokens) -> list:
    """Numero de oracion de cada token. La coma NO corta: hay nombres con
    comas ("DIVISION DE GESTIONES, ADMISIONES Y REGISTROS")."""
    cortes = [m.start() for m in re.finditer(r"[?!.;\n]", consulta)]
    return [bisect_right(cortes, t.ini) for t in tokens]

_A_CARGO = re.compile(r"\ba (?:su )?cargo (?:de|del)\b")
_PERSONA = re.compile(r"\bquien(?:es)?\b")
_ORGANIGRAMA = re.compile(r"\b(?:unidades|dependencias|reparticiones|organigrama)\b"
                          r"|\btipos? de unidad(?:es)?\b")

_PATRONES = (
    ("creacion", re.compile(
        r"\b(?:que|cual|con que|mediante que) (?:resolucion|norma|decreto|documento"
        r"|disposicion)\b(?: \w+){0,3} (?:creo|crea|creada|creado|fundo|establecio)\b"
        r"|\b(?:resolucion|norma|decreto) de creacion\b"
        r"|\bcuando (?:se )?(?:creo|fundo|fue creada|fue creado|fue fundada|fue fundado)\b"
        r"|\bbase legal\b")),
    ("jerarquia", re.compile(
        r"\bde (?:quien|que unidad|que|cual unidad) depende\b"
        r"|\ba (?:que|cual|quien) (?:unidad )?(?:pertenece|reporta)\b"
        r"|\bunidad superior\b|\bsuperior jerarquico\b")),
    ("funciones", re.compile(r"\b(?:funciones|atribuciones|competencias)\b")),
    ("cargos", re.compile(r"\b(?:cargos?|puestos?)\b")),
    ("describir", re.compile(
        r"\bde que se (?:encarga|ocupa)\b|\b(?:objetivo|mision|proposito|finalidad)\b"
        r"|\bque hace\b|\ba que se dedica\b|\bpara que sirve\b")),
    ("contar", re.compile(
        r"\bcuant[ao]s (?:(?!resoluci|documento|norma)\w+ ){0,2}?"
        r"(?:unidades|dependencias|reparticiones)\b"
        r"|\b(?:numero|cantidad|total) de (?:unidades|dependencias|reparticiones)\b"
        r"|\bcuant[ao]s (?:\w+ )?dependen\b")),
    ("listar", re.compile(
        r"\b(?:dame|dime|lista|listame|listar|enumera|enumerame|muestra|muestrame"
        r"|mostrame|nombra|nombrame)\b(?: \w+){0,3} (?:unidades|dependencias"
        r"|reparticiones)\b"
        r"|\b(?:que|cuales) (?:son )?(?:las |los )?(?:unidades|dependencias"
        r"|reparticiones|tipos de unidad(?:es)?)\b"
        r"|\btodas las unidades\b")),
)

_DEPENDEN_DE = re.compile(
    r"\b(?:dependen|depende|cuelgan|cuelga|pertenecen|pertenece"
    r"|reportan|reporta)\b\s+(?:de|del|a|al)\b"
    r"|\ba cargo de\b")

_DIRECTAS = re.compile(r"\bdirecta(?:s|mente)?\b|\binmediata(?:s|mente)?\b")
_RAMA = re.compile(r"\btoda (?:la|su) rama\b|\bcualquier nivel\b|\btodos los niveles\b"
                   r"|\ben total\b|\bincluyendo\b")
_AGRUPAR_TIPO = re.compile(r"\bcada tipo\b|\bpor tipo\b|\btipos de unidad(?:es)?\b"
                           r"|\bque tipos\b")
_AGRUPAR_RELACION = re.compile(r"\bcada relacion\b|\bpor relacion\b"
                               r"|\btipos de relacion\b")

def _intencion(texto):
    """(tipo, ini, fin) del patron que decide, en caracteres de `texto`."""
    if _A_CARGO.search(texto):
        texto = _A_CARGO.sub(lambda m: "x" * len(m.group()), texto)
    for tipo, patron in _PATRONES:
        m = patron.search(texto)
        if m:
            if tipo == "cargos" and _PERSONA.search(texto):
                return None
            return tipo, m.start(), m.end()
    return None

def _cabezas(conn) -> set:
    """Primeras palabras de los nombres de unidad, sin los prefijos de siglas
    ('FCPN-CARR-INSTITUTO ...' empieza en 'instituto')."""
    cabezas = set(_CABEZAS_FIJAS)
    for (nombre,) in conn.execute("SELECT nombre FROM unidad"):
        partes = [p.strip() for p in re.split(r"\s*-\s*", nombre or "") if p.strip()]
        while len(partes) > 1 and re.fullmatch(r"[A-ZÁÉÍÓÚÑ]{1,7}", partes[0]):
            partes.pop(0)
        toks = _tokens(" ".join(partes))
        if toks and len(toks[0]) >= 5 and toks[0].isalpha():
            cabezas.add(toks[0])
    return cabezas

def _contiene(nombre, buscado) -> bool:
    n = len(buscado)
    return any(nombre[k:k + n] == buscado for k in range(len(nombre) - n + 1))

class _Buscador:
    """replica.buscar por UN token (la frase entera falla con los dobles
    espacios de algunos nombres) y el resto del cotejo en Python."""

    def __init__(self, conn):
        self.conn = conn
        self._cache = {}

    def unidades_que_contienen(self, contenido) -> list:
        clave = max(contenido, key=len)
        if clave not in self._cache:
            self._cache[clave] = [
                (fila, _contenido(_tokens(fila["nombre"])))
                for fila in replica.buscar(self.conn, clave, limite=100_000)]
        if len(contenido) == 1:
            return [(f, n) for f, n in self._cache[clave] if n == contenido]
        return [(f, n) for f, n in self._cache[clave] if _contiene(n, contenido)]

@dataclass(frozen=True)
class _Mencion:
    ini: int
    fin: int
    largo: int
    unidad: object = None
    candidatos: tuple = ()

def _elegir(filas, contenido):
    """La unidad nombrada, o (None, ids) si empatan. Gana el nombre exacto y,
    si no hay, el que agrega menos palabras a lo que escribio el usuario."""
    exactas = [(f, n) for f, n in filas if n == contenido]
    grupo = exactas or filas
    menor = min(len(n) for _, n in grupo)
    mejores = [f for f, n in grupo if len(n) == menor]
    if len(mejores) == 1:
        return mejores[0], ()
    return None, tuple(sorted(f["id"] for f in mejores))

def _menciones(tokens, frases, buscador, cabezas) -> list:
    salida = []
    normas = [t.norma for t in tokens]
    for i, norma in enumerate(normas):
        if norma not in cabezas:
            continue
        fin_frase = i
        while fin_frase < len(normas) and frases[fin_frase] == frases[i]:
            fin_frase += 1
        mencion = None
        for fin in range(fin_frase, i, -1):
            if normas[fin - 1] in _CONECTORES:
                continue
            contenido = _contenido(normas[i:fin])
            filas = buscador.unidades_que_contienen(contenido)
            if filas:
                unidad, candidatos = _elegir(filas, contenido)
                mencion = _Mencion(i, fin, len(contenido), unidad, candidatos)
                break
        if mencion is None:
            ultimo = None
            for k in range(i + 1, fin_frase):
                if normas[k] in _CORTES:
                    break
                if normas[k] not in _CONECTORES:
                    ultimo = k
            if ultimo is not None:
                mencion = _Mencion(i, ultimo + 1, 0)
        if mencion is not None:
            salida.append(mencion)
    return salida

def _brecha(normas, ancla, mencion) -> list:
    a_ini, a_fin = ancla
    if mencion.ini >= a_fin:
        return normas[a_fin:mencion.ini]
    if mencion.fin <= a_ini:
        return normas[mencion.fin:a_ini]
    return []

def _elegir_mencion(normas, tipo, ancla, menciones):
    validas = []
    for m in menciones:
        brecha = _brecha(normas, ancla, m)
        if tipo in SOBRE_UNA_UNIDAD:
            if len(brecha) > 5 or any(t not in _PEGAMENTO for t in brecha):
                continue
        elif any(t in _DOCUMENTOS for t in brecha):
            continue
        validas.append(m)
    if not validas:
        return None
    return max(validas, key=lambda m: (m.largo > 0, m.largo, -m.ini))

_SUFIJOS = frozenset({"", "o", "a", "os", "as", "e", "es", "s"})

def _coincidencias(valor, palabras) -> int:
    """Cuantas palabras del valor ('Sustantivo (Academico)') aparecen en la
    consulta con una flexion ('academicas'). 'organigrama' NO es 'organo'."""
    n = 0
    for t in _contenido(_tokens(valor)):
        if len(t) < 4:
            continue
        raiz = t[:-1] if len(t) > 5 and t[-1] in "aeo" else t
        if any(p.startswith(raiz) and p[len(raiz):] in _SUFIJOS for p in palabras):
            n += 1
    return n

def _filtro(conn, palabras):
    hallados = []
    for fila in replica.tipos(conn):
        n = _coincidencias(fila["tipo"], palabras)
        if n:
            hallados.append((n, Filtro("tipo", fila["tipo"], fila["tipo"])))
    for fila in replica.relaciones(conn):
        n = _coincidencias(fila["etiqueta"], palabras)
        if n:
            hallados.append((n, Filtro("relacion", fila["relacion"], fila["etiqueta"])))
    if not hallados:
        return None
    hallados.sort(key=lambda h: -h[0])
    if len(hallados) > 1 and hallados[0][0] == hallados[1][0]:
        return None
    return hallados[0][1]

def detectar(consulta, conn):
    """Intencion de organigrama de la consulta, o None.

    conn: replica.abrir_lectura(). Sin replica, vacia o ilegible: None."""
    if conn is None or not (consulta or "").strip():
        return None
    try:
        if not replica.contar_unidades(conn):
            return None
        return _detectar(consulta, conn)
    except sqlite3.Error:
        return None

def _detectar(consulta, conn):
    tokens = _tokens_con_posicion(consulta)
    normas = [t.norma for t in tokens]
    texto = " ".join(normas)
    decidido = _intencion(texto)
    if decidido is None:
        return None
    tipo, c_ini, c_fin = decidido
    if tipo in SOBRE_UNA_UNIDAD:
        ajenas = _ACTOS if tipo == "creacion" else _ACTOS | _DOCUMENTOS
        if any(n in ajenas for n in normas):
            return None

    inicios = []
    posicion = 0
    for norma in normas:
        inicios.append(posicion)
        posicion += len(norma) + 1
    ancla = (bisect_right(inicios, c_ini) - 1, bisect_right(inicios, c_fin - 1))

    menciones = _menciones(tokens, _frases(consulta, tokens), _Buscador(conn),
                           _cabezas(conn))
    mencion = _elegir_mencion(normas, tipo, ancla, menciones)
    if tipo in SOBRE_UNA_UNIDAD and mencion is None:
        return None
    if tipo == "contar" and mencion is None and not _ORGANIGRAMA.search(texto):
        return None
    if (tipo in ("contar", "listar") and mencion is None
            and _DEPENDEN_DE.search(texto)):
        return None

    datos = {"tipo": tipo}
    if mencion is not None:
        datos["mencion"] = consulta[tokens[mencion.ini].ini:tokens[mencion.fin - 1].fin]
        datos["candidatos"] = mencion.candidatos
        if mencion.unidad is not None:
            datos["unidad_id"] = mencion.unidad["id"]
            datos["unidad_nombre"] = mencion.unidad["nombre"]
    if tipo in ("contar", "listar"):
        fuera = set(range(mencion.ini, mencion.fin)) if mencion is not None else set()
        datos["filtro"] = _filtro(conn, [n for k, n in enumerate(normas)
                                         if k not in fuera])
        if mencion is not None:
            if _DIRECTAS.search(texto):
                datos["alcance"] = "directas"
            elif _RAMA.search(texto):
                datos["alcance"] = "rama"
        if _AGRUPAR_TIPO.search(texto):
            datos["agrupar"] = "tipo"
        elif _AGRUPAR_RELACION.search(texto):
            datos["agrupar"] = "relacion"
    return Intencion(**datos)
