"""
API de CARGA MANUAL de documentos (se monta dentro de api_gaceta.py).

  GET    /carga                                -> la pagina para cargar (HTML)
  GET    /api/carga/estado                     -> OCR, LLM, cola, documentos
  POST   /api/carga/archivo                    -> sube PDF/imagen/.txt/.md/.json
  POST   /api/carga/texto                      -> informacion escrita a mano
  GET    /api/carga/trabajos                   -> lista de trabajos
  GET    /api/carga/trabajos/{id}              -> un trabajo con su borrador
  GET    /api/carga/trabajos/{id}/ocr          -> el Markdown del OCR
  POST   /api/carga/trabajos/{id}/confirmar    -> revisado: a indexar
  POST   /api/carga/trabajos/{id}/reintentar   -> reintenta uno con error
  DELETE /api/carga/trabajos/{id}              -> descarta un borrador
  GET    /api/carga/documentos                 -> lo cargado a mano
  GET    /api/carga/documentos/{doc_id}        -> detalle + estado en el indice
  PUT    /api/carga/documentos/{doc_id}        -> corrige y reindexa
  DELETE /api/carga/documentos/{doc_id}        -> lo saca de la base
  GET    /api/carga/documentos/{doc_id}/raw    -> el JSON (se puede reimportar)
  GET    /api/carga/documentos/{doc_id}/original -> el archivo subido
  POST   /api/carga/documentos/{doc_id}/verificar -> vuelve a comprobarlo
  POST   /api/carga/grafo                      -> pasa por el LLM lo pendiente

TODOS piden token, siempre (header X-Admin-Token), igual que los de MOF: un
POST abierto que escribe en el indice es una puerta para meterle al chat
cualquier texto como si fuera una resolucion. Y no alcanza con escuchar solo
en 127.0.0.1: un multipart/form-data es un pedido "simple" para el navegador,
asi que cualquier pagina web abierta en la misma maquina podria subir un
archivo aqui. El header no lo puede poner esa pagina.

Vale GACETA_CARGA_TOKEN (recomendado: sirve SOLO para esto) o
GACETA_ADMIN_TOKEN. Sin ninguno, 503. En Docker el arranque genera uno y lo
muestra (docker/arrancar.py).
"""
import os
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

from gaceta_rag.carga import cargador as mod_cargador

PAGINA = Path(__file__).parent / "gaceta_rag" / "carga" / "pagina.html"

def requerir_token_carga(x_admin_token: str | None = Header(default=None)):
    validos = {os.environ.get("GACETA_CARGA_TOKEN"),
               os.environ.get("GACETA_ADMIN_TOKEN")}
    validos.discard(None)
    validos.discard("")
    if not validos:
        raise HTTPException(
            status_code=503,
            detail=("La carga de documentos esta deshabilitada: no hay token. "
                    "Defini GACETA_CARGA_TOKEN (o GACETA_ADMIN_TOKEN) en el "
                    ".env y reinicia."))
    if x_admin_token not in validos:
        raise HTTPException(status_code=401,
                            detail="Token invalido o faltante (header X-Admin-Token).")

def _bool(valor, defecto=False) -> bool:
    if valor is None or valor == "":
        return defecto
    if isinstance(valor, bool):
        return valor
    return str(valor).strip().lower() in ("1", "true", "si", "sí", "on", "yes")

def crear_router(obtener_cargador) -> APIRouter:
    """obtener_cargador() -> el Cargador (o None si todavia no existe)."""
    r = APIRouter()
    protegido = [Depends(requerir_token_carga)]

    def c():
        cg = obtener_cargador()
        if cg is None:
            raise HTTPException(status_code=503, detail="La carga todavia no esta lista.")
        return cg

    def errores(fn, *a, **kw):
        """Excepciones del cargador -> codigos HTTP con un mensaje legible."""
        try:
            return fn(*a, **kw)
        except mod_cargador.RevisionConErrores as exc:
            return JSONResponse({"detail": str(exc), "revision": exc.revision},
                                status_code=422)
        except mod_cargador.NoEncontrado as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except mod_cargador.Conflicto as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except mod_cargador.ErrorCarga as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.get("/carga", response_class=HTMLResponse, include_in_schema=False)
    def pagina():
        return HTMLResponse(PAGINA.read_text(encoding="utf-8"))

    @r.get("/api/carga/estado", dependencies=protegido)
    def estado():
        return c().estado()

    @r.post("/api/carga/archivo", dependencies=protegido, status_code=202)
    async def subir_archivo(
        archivo: UploadFile = File(..., description="PDF, imagen, .txt, .md o .json"),
        modo: str = Form("auto", description="auto | ocr | texto"),
        revisar: str = Form("true", description="true: queda en revision antes de indexar"),
        enriquecer: str = Form("true", description="pasar por el LLM (tema, personas, citas)"),
        titulo: str = Form(None),
        tipo_documento: str = Form(None),
        etiquetas: str = Form(None, description="separadas por coma"),
        paginas_max: int = Form(0, description="0 = todas"),
        permitir_duplicado: str = Form("false"),
    ):
        maximo = mod_cargador.MAX_MB * 1024 * 1024
        datos = bytearray()
        while True:
            trozo = await archivo.read(1024 * 1024)
            if not trozo:
                break
            datos += trozo
            if len(datos) > maximo:
                raise HTTPException(
                    status_code=413,
                    detail=f"El archivo pasa de {mod_cargador.MAX_MB:.0f} MB (CARGA_MAX_MB).")
        trabajos = errores(
            c().crear_desde_archivo, archivo.filename, bytes(datos), modo=modo,
            revisar=_bool(revisar, True), enriquecer=_bool(enriquecer, True),
            titulo=titulo, tipo_documento=tipo_documento, etiquetas=etiquetas,
            paginas_max=paginas_max,
            permitir_duplicado=_bool(permitir_duplicado))
        if isinstance(trabajos, JSONResponse):
            return trabajos
        return {"trabajos": [_resumen(t) for t in trabajos]}

    @r.post("/api/carga/texto", dependencies=protegido, status_code=202)
    async def subir_texto(request: Request):
        """Body: {"titulo", "contenido", "tipo_documento", "etiquetas",
        "revisar": true, "enriquecer": true, "permitir_duplicado": false}"""
        body = await _json(request)
        t = errores(c().crear_desde_texto, body.get("titulo"), body.get("contenido"),
                    tipo_documento=body.get("tipo_documento"),
                    etiquetas=body.get("etiquetas"),
                    revisar=_bool(body.get("revisar"), True),
                    enriquecer=_bool(body.get("enriquecer"), True),
                    permitir_duplicado=_bool(body.get("permitir_duplicado")))
        return t if isinstance(t, JSONResponse) else {"trabajos": [_resumen(t)]}

    @r.get("/api/carga/trabajos", dependencies=protegido)
    def trabajos():
        return {"trabajos": c().trabajos()}

    @r.get("/api/carga/trabajos/{tid}", dependencies=protegido)
    def trabajo(tid: str):
        return errores(c().trabajo, tid)

    @r.get("/api/carga/trabajos/{tid}/ocr", dependencies=protegido,
           response_class=PlainTextResponse)
    def trabajo_ocr(tid: str):
        md = errores(c().markdown_ocr, tid)
        if md is None:
            raise HTTPException(status_code=404, detail="Este trabajo no paso por OCR.")
        return PlainTextResponse(md, media_type="text/markdown; charset=utf-8")

    @r.post("/api/carga/trabajos/{tid}/confirmar", dependencies=protegido)
    async def confirmar(tid: str, request: Request):
        """Body (todo opcional): {"titulo", "contenido", "tipo_documento",
        "etiquetas", "permitir_duplicado", "enriquecer"}"""
        body = await _json(request, vacio_ok=True)
        t = errores(c().confirmar, tid, titulo=body.get("titulo"),
                    contenido=body.get("contenido"),
                    tipo_documento=body.get("tipo_documento"),
                    etiquetas=body.get("etiquetas"),
                    permitir_duplicado=(None if "permitir_duplicado" not in body
                                        else _bool(body["permitir_duplicado"])),
                    enriquecer=(None if "enriquecer" not in body
                                else _bool(body["enriquecer"])))
        return t if isinstance(t, JSONResponse) else _resumen(t)

    @r.post("/api/carga/trabajos/{tid}/reintentar", dependencies=protegido)
    def reintentar(tid: str):
        return _resumen(errores(c().reintentar, tid))

    @r.delete("/api/carga/trabajos/{tid}", dependencies=protegido)
    def descartar(tid: str):
        return errores(c().descartar, tid)

    @r.get("/api/carga/documentos", dependencies=protegido)
    def documentos():
        return {"documentos": c().documentos()}

    @r.get("/api/carga/documentos/{doc_id}", dependencies=protegido)
    def documento(doc_id: str):
        return errores(c().documento, doc_id)

    @r.put("/api/carga/documentos/{doc_id}", dependencies=protegido)
    async def actualizar(doc_id: str, request: Request):
        """Body: {"titulo", "contenido", "tipo_documento", "etiquetas",
        "enriquecer": true, "permitir_duplicado": false} (todo opcional)."""
        body = await _json(request)
        t = errores(c().actualizar, doc_id, titulo=body.get("titulo"),
                    contenido=body.get("contenido"),
                    tipo_documento=body.get("tipo_documento"),
                    etiquetas=body.get("etiquetas"),
                    enriquecer=_bool(body.get("enriquecer"), True),
                    permitir_duplicado=_bool(body.get("permitir_duplicado")))
        return t if isinstance(t, JSONResponse) else _resumen(t)

    @r.delete("/api/carga/documentos/{doc_id}", dependencies=protegido)
    def eliminar(doc_id: str):
        return errores(c().eliminar, doc_id)

    @r.get("/api/carga/documentos/{doc_id}/raw", dependencies=protegido)
    def documento_raw(doc_id: str):
        d = errores(c().documento, doc_id)
        return JSONResponse(d["raw"], headers={
            "Content-Disposition": f'attachment; filename="{doc_id}.json"'})

    @r.get("/api/carga/documentos/{doc_id}/original", dependencies=protegido)
    def documento_original(doc_id: str):
        errores(c().documento, doc_id)
        ruta = c().ruta_original(doc_id)
        if ruta is None:
            raise HTTPException(status_code=404,
                                detail="Ese documento no tiene archivo original guardado.")
        raw = c().documento(doc_id)["raw"]
        nombre = (raw.get("carga") or {}).get("archivo") or ruta.name
        return FileResponse(str(ruta), filename=nombre)

    @r.post("/api/carga/documentos/{doc_id}/verificar", dependencies=protegido)
    def verificar(doc_id: str):
        errores(c().documento, doc_id)
        return c().verificar(doc_id)

    @r.post("/api/carga/grafo", dependencies=protegido, status_code=202)
    async def pedir_grafo(request: Request):
        """Body opcional: {"doc_ids": [...]}. Sin lista: todo lo cargado a mano
        que todavia no paso por el LLM."""
        body = await _json(request, vacio_ok=True)
        docs = errores(c().pedir_grafo, body.get("doc_ids"))
        return {"encolados": docs}

    return r

def _resumen(t: dict) -> dict:
    """Lo que devuelve un POST: sin el texto entero (puede pesar megas)."""
    salida = {k: t.get(k) for k in ("id", "origen", "estado", "doc_id", "error",
                                    "grafo", "revision", "resultado")}
    salida["archivo"] = (t.get("archivo") or {}).get("nombre")
    salida["titulo"] = (t.get("borrador") or {}).get("titulo")
    return salida

async def _json(request: Request, vacio_ok=False) -> dict:
    try:
        cuerpo = await request.body()
        if not cuerpo.strip():
            if vacio_ok:
                return {}
            raise ValueError("vacio")
        import json
        from gaceta_rag.carga.extraccion import decodificar_texto
        body = json.loads(decodificar_texto(cuerpo))
    except Exception:
        raise HTTPException(status_code=400, detail="Body JSON invalido.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="El body tiene que ser un objeto JSON.")
    return body
