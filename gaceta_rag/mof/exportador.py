# -*- coding: utf-8 -*-
"""
Unidad de MOF -> ficha con el esquema de un raw doc de la gaceta.

Por que una "ficha" y no una tabla aparte: el pipeline ya sabe indexar
data/raw/doc_NNNNN.json (chunks + embeddings + FTS + titulos de las citas).
Si la unidad entra con ESE esquema, se indexa sola y aparece en las respuestas
con su titulo bien puesto, sin tocar el motor.

Tres decisiones que NO son cosmeticas
─────────────────────────────────────
1. doc_id RESERVADO. generar_base.py:639 NO usa el nombre del archivo: arma el
   doc_id como f"doc_{paperlessId:05d}". Asi que la ficha lleva un
   paperlessId propio en un rango reservado (RANGO_ID_BASE + unidad_id) que no
   colisiona con los ids de Paperless (el corpus llega hoy a 3.312). Ademas el
   prefijo 'doc_9' las hace identificables y borrables en bloque.

2. CABECERA PROTEGIDA. construir_adyacencia.numero_de_raw() lee los primeros
   250 caracteres del contenido buscando "No.NNN/AAAA" para deducir el numero
   PROPIO del documento. Una ficha nombra resoluciones (su base legal): si una
   de ellas cayera en la cabecera, la unidad quedaria indexada COMO SI FUERA
   esa resolucion y ensuciaria el grafo de citas. Por eso la base legal va al
   final y _proteger_cabecera() lo verifica de verdad, no por convencion.

3. SIN DATOS PERSONALES. La tabla `persona` de MOF trae CI, email, celular,
   direccion, fecha de nacimiento y foto. Al indice entra SOLO el nombre y el
   cargo: el filtro esta aca, en el exportador, no aguas abajo (ver
   PRIVACIDAD.md). CAMPOS_PERSONA_PERMITIDOS es la lista blanca.
"""
import hashlib
import re
import unicodedata

RANGO_ID_BASE = 900_000
RANGO_ID_MAX = 99_999

TIPO_DOCUMENTO = "Manual de Organizacion y Funciones"
ETIQUETA_MOF = "MOF"

CAMPOS_PERSONA_PERMITIDOS = ("nombre", "paterno", "materno")

MARGEN_CABECERA = 400

_HDR = re.compile(r"No\.?\s*(\d{1,4})\s*[/-]\s*(20\d{2})")
_TIT_NUM = re.compile(
    r"(?:HCF|HCU|HCC|HCD|RHCF|RR)[\s/_.-]*(\d{1,4})[\s/_.-]+(20\d{2})", re.I)

_SEPARADOR = "-" * 60

_ORDINAL = re.compile(r"^\s*\d{1,3}\s*[.)](?:\s*[-.])?\s+")

class ErrorExportacion(ValueError):
    """La unidad no se puede convertir en ficha (id fuera de rango, etc.)."""

def doc_id_de_unidad(unidad_id) -> str:
    """'doc_900012' para la unidad 12. El nombre del archivo debe ser este
    mismo id (_CacheTitulos abre raw/<doc_id>.json para el titulo de la cita)."""
    try:
        n = int(unidad_id)
    except (TypeError, ValueError):
        raise ErrorExportacion(f"unidad_id no numerico: {unidad_id!r}")
    if not 1 <= n <= RANGO_ID_MAX:
        raise ErrorExportacion(
            f"unidad_id {n} fuera del rango 1..{RANGO_ID_MAX}")
    return f"doc_{RANGO_ID_BASE + n:05d}"

def es_ficha_mof(doc_id) -> bool:
    """True si el doc_id pertenece al rango reservado de MOF."""
    m = re.fullmatch(r"doc_(\d+)", str(doc_id or ""))
    if not m:
        return False
    n = int(m.group(1))
    return RANGO_ID_BASE < n <= RANGO_ID_BASE + RANGO_ID_MAX

def unidad_id_de_doc(doc_id):
    """Camino inverso de doc_id_de_unidad(); None si no es una ficha de MOF."""
    if not es_ficha_mof(doc_id):
        return None
    return int(str(doc_id).split("_")[1]) - RANGO_ID_BASE

def huella(ficha: dict) -> str:
    """Huella del contenido de la ficha, igual que generar_base.hash_contenido.
    Sirve para saber si un evento de MOF cambia algo de verdad."""
    return hashlib.sha1(
        (ficha.get("contenido") or "").encode("utf-8", "ignore")
    ).hexdigest()[:12]

def _txt(v) -> str:
    return str(v).strip() if v is not None else ""

def _nombre_persona(persona: dict) -> str:
    """Nombre completo con la lista blanca. Nunca CI, email ni telefono."""
    partes = [_txt((persona or {}).get(c)) for c in CAMPOS_PERSONA_PERMITIDOS]
    return " ".join(p for p in partes if p)

def _sin_marcador_resolucion(texto: str) -> str:
    """Quita el 'No.' delante de un numero de resolucion.

    'Resolucion HCF No. 393/2022' -> 'Resolucion HCF 393/2022'. El numero se
    conserva entero (lo necesita el cruce con el corpus, ver enlaces.py); lo
    que se saca es el marcador que hace que un lector automatico lo lea como
    el numero PROPIO del documento.
    """
    return re.sub(r"\bN[o°º]\.?\s*(?=\d{1,4}\s*[/-]\s*20\d{2})", "", texto or "",
                  flags=re.I)

def _proteger_cabecera(texto: str) -> str:
    """Garantiza que los primeros MARGEN_CABECERA chars no contengan un numero
    de resolucion con marcador. Si lo hubiera, empuja el cuerpo hacia abajo con
    un separador en vez de mutilar el dato."""
    if not _HDR.search(texto[:MARGEN_CABECERA]):
        return texto
    relleno = _SEPARADOR + "\n"
    protegido = texto
    for _ in range(8):
        protegido = relleno + protegido
        if not _HDR.search(protegido[:MARGEN_CABECERA]):
            return protegido
    raise ErrorExportacion(
        "no se pudo despejar la cabecera: hay un numero de resolucion "
        "demasiado arriba en la ficha")

_ROMPE_ORG = re.compile(r"\b(HCF|HCU|HCC|HCD|RHCF|RR)([\s/_.-]+)(?=\d)", re.I)

def _titulo(unidad: dict) -> str:
    """'MOF - Instituto de Investigaciones (IIQ)'.

    Sin numero de resolucion legible: identidad.identidad_titulo() y
    construir_adyacencia.numero_de_raw() deducen del TITULO el numero PROPIO
    del documento, y una ficha no tiene ninguno. Una unidad puede llamarse
    'Comision HCF 393/2022 de Evaluacion' — nombre real, no invento — y sin
    esto quedaria indexada como si fuera esa resolucion.
    """
    nombre = _txt(unidad.get("nombre")) or f"Unidad {unidad.get('id')}"
    sigla = _txt(unidad.get("sigla"))
    base = f"{ETIQUETA_MOF} - {nombre}"
    if sigla:
        base = f"{base} ({sigla})"
    return _ROMPE_ORG.sub(lambda m: f"{m.group(1)}: ", base)

def _cat(unidad: dict, clave: str) -> str:
    """Nombre de un catalogo anidado ({'tipo': {'nombre': 'Instituto'}}) o
    del campo plano equivalente ('tipo_nombre')."""
    v = unidad.get(clave)
    if isinstance(v, dict):
        return _txt(v.get("nombre") or v.get("descripcion"))
    plano = unidad.get(f"{clave}_nombre")
    return _txt(plano if plano is not None else v)

def _clase_id(unidad: dict) -> str:
    """Id de la clase de unidad: lo unico que ambos endpoints dan igual."""
    v = unidad.get("tipoUnidad")
    if isinstance(v, dict):
        return _txt(v.get("id"))
    return _txt(v)

def _clase_texto(unidad: dict) -> str:
    """Clase legible: 'tipo_unidad' en el listado, 'clase' en el detalle."""
    return (_txt(unidad.get("tipo_unidad")) or _txt(unidad.get("clase"))
            or _cat(unidad, "tipoUnidad"))

def _relacion_codigo(unidad: dict) -> str:
    """Codigo de la relacion ('L'): presente en los dos endpoints."""
    v = unidad.get("relacion")
    if isinstance(v, dict):
        return _txt(v.get("codigo") or v.get("nombre") or v.get("descripcion"))
    return _txt(v)

def _relacion_texto(unidad: dict) -> str:
    """Relacion legible ('Linea'). Solo el listado la trae; el detalle da el
    codigo y con eso se queda."""
    return _txt(unidad.get("str_relacion")) or _relacion_codigo(unidad)

def _padre_id(unidad: dict, padre: dict = None) -> str:
    """Id de la unidad superior. En el listado `parent` ES el id; en el detalle
    es un objeto. Comparar por id evita depender de como venga el nombre."""
    p = unidad.get("parent")
    if isinstance(p, dict):
        return _txt(p.get("id"))
    if p not in (None, ""):
        return _txt(p)
    return _txt((padre or {}).get("id")) or _txt(unidad.get("parentId"))

def _nombre_padre(unidad: dict, padre: dict = None) -> str:
    """Nombre de la unidad superior: del padre ya resuelto, del objeto `parent`
    del detalle, o del campo `dependencia` del listado."""
    if padre:
        return _txt(padre.get("nombre"))
    p = unidad.get("parent")
    if isinstance(p, dict):
        return _txt(p.get("nombre"))
    return _txt(unidad.get("dependencia"))

def proyeccion_unidad(unidad: dict, padre: dict = None) -> dict:
    """Los campos propios de la unidad que el listado de MOF si trae,
    normalizados.

    Es la base de la comparacion rapida de la reconciliacion. Todo lo que entra
    aqui tiene que poder leerse igual desde el listado y desde el detalle: un
    campo que solo exista en uno de los dos haria que cada corrida creyera ver
    un cambio. Por eso NO estan las funciones ni el personal — viven en otras
    tablas y solo los ve la pasada profunda.
    """
    unidad = unidad or {}
    return {
        "nombre": _txt(unidad.get("nombre")),
        "sigla": _txt(unidad.get("sigla")),
        "codigo": _txt(unidad.get("codigo")),
        "objetivo": _txt(unidad.get("objetivo")),
        "oficial": bool(unidad.get("oficial")),
        "tipo": _cat(unidad, "tipo"),
        "nivel": _cat(unidad, "nivel"),
        "relacion": _relacion_codigo(unidad),
        "clase": _clase_id(unidad),
        "padre": _padre_id(unidad, padre),
        "res_creacion": _txt(unidad.get("resCreacion")
                             or unidad.get("res_creacion")),
        "base_legal": _txt(unidad.get("baseLegal") or unidad.get("base_legal")),
        "fec_creacion": _txt(unidad.get("fecCreacion")
                             or unidad.get("fec_creacion"))[:10],
    }

def cuerpo_ficha(unidad: dict, funciones=None, personal=None,
                 padre: dict = None) -> str:
    """Texto plano de la ficha. Orden deliberado: identidad y objetivo arriba
    (es lo que responde "de que se encarga X"), base legal al final (es lo que
    NO debe caer en la cabecera)."""
    unidad = unidad or {}
    lineas = []

    nombre = _txt(unidad.get("nombre")) or f"Unidad {unidad.get('id')}"
    lineas.append(f"UNIDAD ORGANIZACIONAL: {nombre}")

    ident = []
    if _txt(unidad.get("sigla")):
        ident.append(f"SIGLA: {_txt(unidad['sigla'])}")
    if _txt(unidad.get("codigo")):
        ident.append(f"CODIGO: {_txt(unidad['codigo'])}")
    if ident:
        lineas.append(" | ".join(ident))

    clasif = []
    for etiqueta, valor in (("TIPO", _cat(unidad, "tipo")),
                            ("NIVEL", _cat(unidad, "nivel")),
                            ("RELACION", _relacion_texto(unidad)),
                            ("CLASE", _clase_texto(unidad))):
        if valor:
            clasif.append(f"{etiqueta}: {valor}")
    if clasif:
        lineas.append(" | ".join(clasif))

    nombre_padre = _nombre_padre(unidad, padre)
    if nombre_padre:
        s = _txt((padre or {}).get("sigla"))
        lineas.append(f"DEPENDE DE: {nombre_padre}" + (f" ({s})" if s else ""))

    if unidad.get("oficial") is not None:
        lineas.append("ESTADO: unidad oficial" if unidad.get("oficial")
                      else "ESTADO: unidad no oficializada")

    objetivo = _txt(unidad.get("objetivo"))
    if objetivo:
        lineas += ["", "OBJETIVO:", objetivo]

    funciones = list(funciones or [])
    if funciones:
        numeradas = []
        for f in funciones:
            texto = _ORDINAL.sub("", _txt((f or {}).get("funcion")))
            if texto:
                numeradas.append(f"{len(numeradas) + 1}. {texto}")
        if numeradas:
            lineas += ["", "FUNCIONES:"] + numeradas

    filas = []
    for asig in (personal or []):
        asig = asig or {}
        persona = _nombre_persona(asig.get("persona") or {})
        cargo = _txt((asig.get("cargo") or {}).get("nombre")
                     if isinstance(asig.get("cargo"), dict)
                     else asig.get("cargo"))
        cargo = cargo or _txt(asig.get("nombre")) or _txt(asig.get("cargo_nombre"))
        if persona and cargo:
            filas.append(f"- {cargo}: {persona}")
        elif cargo:
            filas.append(f"- {cargo}")
        elif persona:
            filas.append(f"- {persona}")
    if filas:
        lineas += ["", "CARGOS DE LA UNIDAD:"] + filas

    legal = []
    res = _sin_marcador_resolucion(_txt(unidad.get("resCreacion")
                                        or unidad.get("res_creacion")))
    if res:
        legal.append(f"Resolucion de creacion: {res}")
    if _txt(unidad.get("fecCreacion") or unidad.get("fec_creacion")):
        legal.append("Fecha de creacion: "
                     + _txt(unidad.get("fecCreacion")
                            or unidad.get("fec_creacion"))[:10])
    base = _sin_marcador_resolucion(_txt(unidad.get("baseLegal")
                                         or unidad.get("base_legal")))
    if base:
        legal.append(f"Base legal: {base}")
    for f in funciones:
        bl = _sin_marcador_resolucion(_txt((f or {}).get("baseLegal")
                                           or (f or {}).get("base_legal")))
        if bl and bl not in legal:
            legal.append(f"Base legal de una funcion: {bl}")
    if legal:
        lineas += ["", "BASE LEGAL:"] + legal

    return _proteger_cabecera("\n".join(lineas).strip() + "\n")

def ficha_de_unidad(unidad: dict, funciones=None, personal=None,
                    padre: dict = None, facultad: str = None) -> dict:
    """Raw doc listo para escribir en data/raw/<doc_id>.json.

    Mismo esquema que un documento de Paperless, con un bloque 'mof' extra
    para poder reconciliar despues (id de la unidad y su updated_at).
    """
    unidad = unidad or {}
    doc_id = doc_id_de_unidad(unidad.get("id"))
    pid = int(doc_id.split("_")[1])

    etiquetas = [ETIQUETA_MOF]
    if _txt(unidad.get("sigla")):
        etiquetas.append(_txt(unidad["sigla"]))
    creacion = referencia_creacion(unidad)

    ficha = {
        "contenido": cuerpo_ficha(unidad, funciones, personal, padre),
        "paperlessId": pid,
        "id": pid,
        "titulo": _titulo(unidad),
        "tipo_documento": TIPO_DOCUMENTO,
        "etiquetas": etiquetas,
        "nroPaginas": 1,
        "vista_previa_imagenes": [],
        "mof": {
            "unidad_id": int(unidad["id"]),
            "updated_at": _txt(unidad.get("updatedAt")
                               or unidad.get("updated_at")) or None,
            "referencias": [list(r) for r in
                            referencias_legales(unidad, funciones)],
            "referencia_creacion": list(creacion) if creacion else None,
        },
    }
    if facultad:
        ficha["campos_personalizados"] = {"Facultad": facultad}
    ficha["mof"]["hash"] = huella(ficha)
    return ficha

_ORGANOS_REF = r"R\.?H\.?C\.?F|H\.?C\.?F|H\.?C\.?U|H\.?C\.?C|H\.?C\.?D|R\.?R"
_REF = re.compile(
    r"(?:\b(" + _ORGANOS_REF + r")\.?[\s./_-]*)?"
    r"(?:N[º°o]?\.?\s*)?"
    r"(\d{1,4})\s*(?:[/-]|de[l]?\s+)\s*(20\d{2})", re.I)
_GATILLO = re.compile(r"resoluci[oó]n|\bres\b|\bR\.?H\.?C", re.I)

def _refs_de(texto: str) -> list:
    """[(organo|None, numero, anio)] de un texto legal, sin duplicados.

    Solo actua cuando el texto habla de resoluciones: una fecha suelta
    '12/2024' no debe convertirse en una arista del grafo.
    """
    texto = _txt(texto)
    if not texto or not _GATILLO.search(texto):
        return []
    refs, vistos = [], set()
    for m in _REF.finditer(texto):
        org = m.group(1)
        if org:
            org = re.sub(r"[.\s_-]", "", org).upper()
            if org == "RHCF":
                org = "HCF"
        clave = (org, str(int(m.group(2))), m.group(3))
        if clave not in vistos:
            vistos.add(clave)
            refs.append(clave)
    return refs

def referencia_creacion(unidad: dict):
    """La resolucion que CREO la unidad, o None.

    Sale unicamente de `res_creacion`. Existe aparte de referencias_legales()
    porque la diferencia es factual, no de orden: una unidad puede tener base
    legal y no tener resolucion de creacion, y en ese caso NINGUNA de sus
    normas la creo. Deducir "la primera es la que crea" convertia una base
    legal cualquiera en una afirmacion falsa dentro del grafo.
    """
    refs = _refs_de((unidad or {}).get("resCreacion")
                    or (unidad or {}).get("res_creacion"))
    return refs[0] if refs else None

def referencias_legales(unidad: dict, funciones=None) -> list:
    """[(organo|None, numero, anio), ...] citados en la base legal de la unidad.

    Solo mira los campos legales, no el objetivo ni el texto libre de las
    funciones. La de creacion, si la hay, va primera.
    """
    unidad = unidad or {}
    fuentes = [
        _txt(unidad.get("resCreacion") or unidad.get("res_creacion")),
        _txt(unidad.get("baseLegal") or unidad.get("base_legal")),
    ]
    for f in (funciones or []):
        fuentes.append(_txt((f or {}).get("baseLegal") or (f or {}).get("base_legal")))

    refs, vistos = [], set()
    for texto in fuentes:
        for clave in _refs_de(texto):
            if clave not in vistos:
                vistos.add(clave)
                refs.append(clave)
    return refs

def normalizar(texto: str) -> str:
    """Minusculas sin acentos: para comparar nombres de unidad sin sorpresas."""
    t = unicodedata.normalize("NFKD", str(texto or ""))
    return "".join(c for c in t if not unicodedata.combining(c)).lower().strip()
