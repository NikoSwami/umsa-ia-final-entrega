"""
Guia para mandar documentos por MCP (se monta dentro de api_gaceta.py).

  GET  /guia-mcp          -> la misma guia como pagina, para cualquier persona
  GET  /api/mcp/guia      -> como conectarse y que mandar, con los IDs reales
  POST /api/mcp/revisar   -> dice que esta mal en un pedido ANTES de mandarlo

Para quien todavia no sabe nada, asi que no piden token: ninguno sube nada ni
muestra un secreto. Solo leen el catalogo de Paperless (nombres de etiquetas y
tipos) con el token de solo lectura de la Gaceta. Se ven en /docs.

La subida de verdad la hace el cliente MCP contra paperless-mcp, no la Gaceta:
la Gaceta no tiene, a proposito, un token que pueda escribir en Paperless.
"""
import os
import posixpath
import re
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import HTMLResponse

from gaceta_rag.carga import extraccion
from gaceta_rag.paperless import cliente as mod_cliente

ETIQUETAS = ["Entrada por MCP"]
PAGINA = Path(__file__).parent / "gaceta_rag" / "paperless" / "pagina_guia.html"
CARPETA = "/entrada/"
CAMPOS = {"file", "file_path", "filename", "title", "created", "correspondent",
          "document_type", "storage_path", "tags", "archive_serial_number",
          "custom_fields"}
FORMATOS = sorted(set(extraccion.EXTENSIONES) - extraccion.JSONS)
AYUDA_PREPARAR = "Quien administra la Gaceta tiene que correr docker/preparar_paperless.py."
_RE_WINDOWS = re.compile(r"^[A-Za-z]:[\\/]")

EXPLICACION_CAMPOS = [
    {"campo": "file_path", "requerido": True, "ejemplo": "/entrada/resolucion_987.pdf",
     "que_poner": "La ruta del archivo DENTRO del contenedor: siempre /entrada/ y el "
                  "nombre. Antes hay que copiar el archivo a la carpeta entrada\\ del "
                  "proyecto. Una ruta de tu PC (C:\\...) no sirve."},
    {"campo": "tags", "requerido": True, "ejemplo": [1],
     "que_poner": "Lista de IDs (numeros, no nombres). Tiene que incluir el de la "
                  "etiqueta gaceta: sin ella el documento no pasa a la Gaceta. Las "
                  "demas etiquetas pasan con su nombre."},
    {"campo": "title", "requerido": False, "ejemplo": "Resolución 987",
     "que_poner": "El titulo. Sin el, la Gaceta sugiere uno a partir del texto."},
    {"campo": "document_type", "requerido": False, "ejemplo": 1,
     "que_poner": "El ID del tipo (numero). Sin el, queda como Resolución."},
    {"campo": "created", "requerido": False, "ejemplo": "2026-11-03",
     "que_poner": "La fecha del documento, como AAAA-MM-DD."},
    {"campo": "correspondent", "requerido": False, "ejemplo": 2,
     "que_poner": "El ID de quien lo emite. Se guarda; el chat no lo usa todavia."},
    {"campo": "custom_fields", "requerido": False, "ejemplo": [3],
     "que_poner": "IDs de campos personalizados. Se guardan con su nombre."},
]

EJEMPLOS_REVISAR = {
    "correcto": {
        "summary": "Un pedido correcto",
        "value": {"file_path": "/entrada/resolucion_987.pdf", "title": "Resolución 987",
                  "tags": [1], "document_type": 1, "created": "2026-11-03"}},
    "ruta_de_windows": {
        "summary": "Error común: la ruta de la PC",
        "value": {"file_path": "C:\\Users\\yo\\resolucion_987.pdf", "tags": [1]}},
    "json_rpc": {
        "summary": "El pedido JSON-RPC entero, como lo manda un cliente MCP",
        "value": {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "post_document",
                             "arguments": {"file_path": "/entrada/resolucion_987.pdf",
                                           "title": "Resolución 987", "tags": [1]}}}},
}

def _url_mcp() -> str:
    return f"http://127.0.0.1:{os.environ.get('PAPERLESS_MCP_PUERTO') or 3100}/mcp"

def _pedido(argumentos: dict, id_=3) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "method": "tools/call",
            "params": {"name": "post_document", "arguments": argumentos}}

def _es_id(valor) -> bool:
    return isinstance(valor, int) and not isinstance(valor, bool)

def crear_router(obtener_puente) -> APIRouter:
    """obtener_puente() -> el Puente (de el se usan el cliente y la etiqueta)."""
    r = APIRouter()

    def catalogo() -> dict:
        """Lo que se pudo leer de Paperless, y como esta."""
        puente = obtener_puente()
        salida = {"etiqueta": (puente.etiqueta if puente else "gaceta"),
                  "etiquetas": None, "tipos": None, "puente": "apagado",
                  "paperless": "sin comprobar", "mensaje": None}
        if puente is None or not puente.cliente.configurado:
            salida["mensaje"] = ("La entrada por MCP no esta activa en esta Gaceta. "
                                 + AYUDA_PREPARAR + " Ver DOCKER.md.")
            return salida
        salida["puente"] = "activo"
        try:
            salida["etiquetas"] = puente.cliente.catalogo("tags")
            salida["tipos"] = puente.cliente.catalogo("document_types")
            salida["paperless"] = "responde"
        except mod_cliente.ErrorPaperless as exc:
            salida.update(etiquetas=None, tipos=None, paperless="no responde",
                          mensaje=f"Paperless no responde ({exc}). Los IDs no se "
                                  "pudieron leer: pedilos con list_tags.")
            return salida
        if _id_etiqueta(salida) is None:
            salida["mensaje"] = (f"Paperless no tiene la etiqueta {salida['etiqueta']}. "
                                 + AYUDA_PREPARAR)
        return salida

    @r.get("/guia-mcp", response_class=HTMLResponse, include_in_schema=False)
    def pagina():
        return HTMLResponse(PAGINA.read_text(encoding="utf-8"))

    @r.get("/api/mcp/guia", tags=ETIQUETAS,
           summary="Cómo mandar un documento por MCP, paso a paso")
    def guia():
        """Todo lo que hace falta para subir un documento por MCP, con los IDs reales
        de este Paperless y un pedido de ejemplo listo para copiar. No pide token ni
        muestra ninguno: dice donde esta."""
        c = catalogo()
        id_etiqueta = _id_etiqueta(c)
        tipos = [{"id": i, "nombre": n} for i, n in sorted((c["tipos"] or {}).items())]
        resolucion = next((t["id"] for t in tipos if t["nombre"] == "Resolución"), None)
        argumentos = {"file_path": f"{CARPETA}mi_documento.pdf", "title": "Resolución 987",
                      "tags": [id_etiqueta if id_etiqueta is not None
                               else f"<ID de la etiqueta {c['etiqueta']}>"],
                      "created": date.today().isoformat()}
        if resolucion is not None:
            argumentos["document_type"] = resolucion
        return {
            "que_es": ("Un cliente MCP (Claude u otro asistente) puede dejar documentos "
                       "en la Gaceta. Llegan a /carga en revision y entran al chat "
                       "cuando alguien los confirma."),
            "estado": {k: c[k] for k in ("puente", "paperless", "mensaje")},
            "conexion": {
                "url": _url_mcp(), "metodo": "POST",
                "cabeceras": {"Authorization": "Bearer <PAPERLESS_MCP_API_KEY>",
                              "Content-Type": "application/json",
                              "Accept": "application/json, text/event-stream"},
                "token": ("Es PAPERLESS_MCP_API_KEY, del .env de la Gaceta. Pediselo a "
                          "quien la administra: aqui no se muestra."),
                "claude_code": (f"claude mcp add --transport http paperless {_url_mcp()} "
                                '--header "Authorization: Bearer TU_TOKEN"'),
            },
            "pasos": [
                {"paso": 1, "titulo": "Conectar el cliente MCP",
                 "detalle": f"A {_url_mcp()}, con el token como Bearer (ver conexion)."},
                {"paso": 2, "titulo": "Dejar el archivo en la carpeta entrada",
                 "detalle": ("Copialo a la carpeta entrada\\ del proyecto. Para el MCP "
                             f"esa carpeta se llama {CARPETA}: un PDF no se puede mandar "
                             "entero por el chat del asistente.")},
                {"paso": 3, "titulo": "Averiguar los IDs",
                 "detalle": ("post_document pide numeros, no nombres: list_tags para el "
                             f"de la etiqueta {c['etiqueta']} y list_document_types para el "
                             "tipo. Si estan abajo, ya estan leidos.")},
                {"paso": 4, "titulo": "Revisar el pedido (opcional)",
                 "detalle": "POST /api/mcp/revisar con los argumentos: dice si algo esta mal."},
                {"paso": 5, "titulo": "Llamar a post_document",
                 "detalle": "Con los argumentos de campos. El ejemplo de abajo sirve tal cual."},
                {"paso": 6, "titulo": "Confirmarlo en /carga",
                 "detalle": ("A los pocos segundos aparece en /carga, en revision. Hasta "
                             "que alguien lo confirme, el chat no lo conoce.")},
            ],
            "campos": EXPLICACION_CAMPOS,
            "etiqueta_gaceta": ({"id": id_etiqueta, "nombre": c["etiqueta"]}
                                if id_etiqueta is not None else None),
            "tipos_de_documento": tipos,
            "formatos_aceptados": FORMATOS,
            "ejemplo": _pedido(argumentos),
            "despues": ("Para cambiar titulo, tipo o etiquetas, hacelo en Paperless: se "
                        "aplica solo. Quitarle la etiqueta lo saca del chat. Ver DOCKER.md."),
        }

    @r.post("/api/mcp/revisar", tags=ETIQUETAS,
            summary="Revisar un pedido de post_document antes de mandarlo")
    def revisar(cuerpo: Any = Body(..., openapi_examples=EJEMPLOS_REVISAR)):
        """Recibe los argumentos de post_document (o el pedido JSON-RPC entero) y dice
        que esta mal y como arreglarlo. No manda nada a ningun lado."""
        if not isinstance(cuerpo, dict):
            raise HTTPException(status_code=400, detail="El body tiene que ser un objeto JSON.")
        errores, avisos = [], []

        def error(codigo, mensaje, como):
            errores.append({"codigo": codigo, "mensaje": mensaje, "como_arreglarlo": como})

        def aviso(codigo, mensaje):
            avisos.append({"codigo": codigo, "mensaje": mensaje})

        argumentos = cuerpo
        if "params" in cuerpo or "method" in cuerpo:
            params = cuerpo.get("params") or {}
            if params.get("name") != "post_document":
                error("no_es_post_document", f"La herramienta es {params.get('name')!r}.",
                      "Para subir un documento, la herramienta es post_document.")
            argumentos = params.get("arguments") or {}
        if not isinstance(argumentos, dict):
            raise HTTPException(status_code=400, detail="arguments tiene que ser un objeto JSON.")

        c = catalogo()
        _revisar_archivo(argumentos, error, aviso)
        _revisar_etiquetas(argumentos, c, error)
        _revisar_tipo(argumentos, c, error, aviso)
        _revisar_resto(argumentos, error, aviso)
        if c["etiquetas"] is None:
            aviso("sin_verificar_ids", "No se pudieron comprobar los IDs contra Paperless: "
                  + (c["mensaje"] or ""))
        ok = not errores
        return {"ok": ok, "errores": errores, "avisos": avisos,
                "pedido_listo": _pedido(argumentos) if ok else None}

    return r

def _id_etiqueta(c):
    for id_, nombre in (c["etiquetas"] or {}).items():
        if (nombre or "").casefold() == c["etiqueta"].casefold():
            return id_
    return None

def _revisar_archivo(a, error, aviso):
    ruta, en_base64 = a.get("file_path"), a.get("file")
    if not ruta and not en_base64:
        return error("falta_archivo", "No hay archivo.",
                     f"Usa file_path con la ruta en {CARPETA}, por ejemplo "
                     f"{CARPETA}resolucion_987.pdf.")
    if en_base64 and not ruta:
        aviso("base64", "Mandar el archivo en base64 funciona, pero un PDF no entra en el "
                        f"contexto de un asistente: mejor file_path en {CARPETA}.")
        if not a.get("filename"):
            return error("falta_filename", "Con file hace falta filename.",
                         "Agrega filename con el nombre y la extension, por ejemplo res.pdf.")
        nombre = a["filename"]
    else:
        ruta = str(ruta)
        nombre = PurePosixPath(ruta.replace("\\", "/")).name
        if _RE_WINDOWS.match(ruta) or "\\" in ruta:
            error("ruta_de_windows", "Es una ruta de tu PC; el MCP corre en un contenedor.",
                  f"Copia el archivo a la carpeta entrada\\ del proyecto y usa "
                  f"{CARPETA}{nombre}.")
        elif not posixpath.normpath(ruta).startswith(CARPETA):
            error("fuera_de_entrada", f"Solo se aceptan archivos bajo {CARPETA}.",
                  f"Copia el archivo a la carpeta entrada\\ del proyecto y usa "
                  f"{CARPETA}{nombre}.")
    extension = PurePosixPath(nombre).suffix.lower()
    if extension in extraccion.JSONS:
        error("json_no_se_acepta", "Un .json trae su propio ID y podria pisar el corpus.",
              "Manda el documento original (PDF, imagen o texto).")
    elif extension not in FORMATOS:
        error("formato_no_aceptado", f"El formato {extension or '(sin extension)'} no se acepta.",
              f"Formatos aceptados: {', '.join(FORMATOS)}. Un Word, guardalo como PDF.")

def _revisar_etiquetas(a, c, error):
    tags = a.get("tags")
    if tags is not None and (not isinstance(tags, list) or not all(_es_id(t) for t in tags)):
        return error("etiquetas_no_son_ids", "tags tiene que ser una lista de IDs (numeros).",
                     "Busca los IDs con list_tags; por ejemplo tags: [1].")
    id_etiqueta = _id_etiqueta(c)
    if id_etiqueta is not None and id_etiqueta in (tags or []):
        return
    if id_etiqueta is None and tags:
        return
    como = (f"Agrega el ID {id_etiqueta} (la etiqueta {c['etiqueta']}) a tags."
            if id_etiqueta is not None
            else f"Agrega el ID de la etiqueta {c['etiqueta']} (buscalo con list_tags).")
    error("falta_etiqueta_gaceta",
          f"Sin la etiqueta {c['etiqueta']}, el documento queda en Paperless y no pasa a la Gaceta.",
          como)

def _revisar_tipo(a, c, error, aviso):
    if a.get("document_type") is None:
        return aviso("sin_tipo", "Sin document_type, el documento queda como Resolución.")
    tipo = a["document_type"]
    if not _es_id(tipo):
        return error("tipo_no_es_id", "document_type tiene que ser un ID (numero).",
                     "Busca el ID con list_document_types.")
    if c["tipos"] is not None and tipo not in c["tipos"]:
        nombres = ", ".join(f"{i} = {n}" for i, n in sorted(c["tipos"].items()))
        error("tipo_desconocido", f"No hay ningun tipo con el ID {tipo}.",
              f"Los tipos que existen: {nombres or '(ninguno)'}.")

def _revisar_resto(a, error, aviso):
    if not str(a.get("title") or "").strip():
        aviso("sin_titulo", "Sin title, la Gaceta sugiere uno a partir del texto.")
    if a.get("created") is not None:
        try:
            if not re.match(r"^\d{4}-\d{2}-\d{2}", str(a["created"])):
                raise ValueError
            datetime.fromisoformat(str(a["created"]))
        except ValueError:
            error("fecha_invalida", f"La fecha {a['created']!r} no se entiende.",
                  "Escribila como AAAA-MM-DD, por ejemplo 2026-11-03.")
    for campo in sorted(set(a) - CAMPOS):
        aviso("campo_desconocido", f"{campo!r} no es un campo de post_document y se ignora.")
