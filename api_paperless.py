"""
API del puente con Paperless-ngx (se monta dentro de api_gaceta.py).

  POST /api/paperless/evento   -> el webhook de Paperless avisa {"doc_id": N}
  GET  /api/paperless/estado   -> pendientes, documentos, errores
  POST /api/paperless/reconciliar -> compara ambos lados; {"confirmar": true}
                                     aplica las bajas que la valvula retuvo

Cada token abre solo lo suyo. GACETA_PAPERLESS_TOKEN queda guardado dentro de
Paperless (va en la cabecera del webhook), asi que abre el aviso y nada mas: el
peor uso posible es pedirle a la Gaceta que vuelva a leer un documento que
Paperless igual tiene que confirmar. No abre /api/carga ni el estado.
"""
import json
import os

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from api_carga import requerir_token_carga

def requerir_token_evento(x_admin_token: str | None = Header(default=None)):
    validos = {os.environ.get("GACETA_PAPERLESS_TOKEN"),
               os.environ.get("GACETA_ADMIN_TOKEN")}
    validos.discard(None)
    validos.discard("")
    if not validos:
        raise HTTPException(
            status_code=503,
            detail=("El aviso de Paperless esta deshabilitado: no hay token. "
                    "Defini GACETA_PAPERLESS_TOKEN en el .env y reinicia."))
    if x_admin_token not in validos:
        raise HTTPException(status_code=401,
                            detail="Token invalido o faltante (header X-Admin-Token).")

def crear_router(obtener_puente, obtener_reconciliador=None) -> APIRouter:
    """obtener_puente() -> el Puente; obtener_reconciliador() -> el Reconciliador."""
    r = APIRouter()

    def p():
        puente = obtener_puente()
        if puente is None or not puente.cliente.configurado:
            raise HTTPException(
                status_code=503,
                detail=("El puente con Paperless esta apagado: faltan "
                        "GACETA_PAPERLESS_URL y GACETA_PAPERLESS_API_KEY."))
        return puente

    @r.post("/api/paperless/evento", status_code=202,
            dependencies=[Depends(requerir_token_evento)])
    async def evento(request: Request):
        """Body: {"doc_id": N}. Solo eso: el resto se le pregunta a Paperless."""
        doc_id = _doc_id(await request.body())
        p().recibir_evento(doc_id)
        return {"encolado": doc_id}

    @r.get("/api/paperless/estado", dependencies=[Depends(requerir_token_carga)])
    def estado():
        return p().estado()

    @r.post("/api/paperless/reconciliar", dependencies=[Depends(requerir_token_carga)])
    async def reconciliar(request: Request):
        """Body opcional: {"confirmar": true}. Un booleano de verdad: esto borra."""
        confirmar = _confirmar(await request.body())
        p()
        rec = obtener_reconciliador() if obtener_reconciliador else None
        if rec is None:
            raise HTTPException(status_code=503, detail="La reconciliacion no esta lista.")
        return await run_in_threadpool(rec.ejecutar, confirmar=confirmar)

    return r

def _confirmar(cuerpo: bytes) -> bool:
    if not cuerpo.strip():
        return False
    try:
        datos = json.loads(cuerpo)
    except ValueError:
        raise HTTPException(status_code=400, detail="Body JSON invalido.")
    if (not isinstance(datos, dict) or set(datos) - {"confirmar"}
            or not isinstance(datos.get("confirmar", False), bool)):
        raise HTTPException(status_code=400,
                            detail='El body tiene que ser {"confirmar": true} o nada.')
    return datos.get("confirmar", False)

def _doc_id(cuerpo: bytes) -> int:
    try:
        datos = json.loads(cuerpo)
    except ValueError:
        raise HTTPException(status_code=400, detail="Body JSON invalido.")
    if not isinstance(datos, dict) or set(datos) != {"doc_id"}:
        raise HTTPException(status_code=400, detail='El body tiene que ser {"doc_id": N}.')
    doc_id = datos["doc_id"]
    if isinstance(doc_id, bool) or not isinstance(doc_id, int) or doc_id <= 0:
        raise HTTPException(status_code=400, detail="doc_id tiene que ser un entero positivo.")
    return doc_id
