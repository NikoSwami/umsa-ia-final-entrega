"""
API para CARGAR EL RESPALDO DE MOF (se monta dentro de api_gaceta.py).

  GET    /carga-mof                                -> la pagina (HTML)
  GET    /api/mof/respaldo/estado                  -> replica, motor, ultimo cargado
  POST   /api/mof/respaldo                         -> sube el pg_dump (.sql)
  GET    /api/mof/respaldo/trabajos                -> lista de cargas
  GET    /api/mof/respaldo/trabajos/{id}           -> una carga con su informe
  GET    /api/mof/respaldo/trabajos/{id}/eliminado -> SOLO lo que se elimina
  POST   /api/mof/respaldo/trabajos/{id}/confirmar -> sigue una detenida por bajas
  DELETE /api/mof/respaldo/trabajos/{id}           -> descarta una detenida

Mismo token que la carga manual (X-Admin-Token = GACETA_CARGA_TOKEN o
GACETA_ADMIN_TOKEN) y por la misma razon: esto reescribe el organigrama que el
chat da por cierto. Ver gaceta_rag/mof/carga_respaldo.py para los pasos.
"""
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

from api_carga import _bool, requerir_token_carga
from gaceta_rag.mof import carga_respaldo as mod

PAGINA = Path(__file__).parent / "gaceta_rag" / "mof" / "pagina_respaldo.html"

def crear_router(obtener_cargador) -> APIRouter:
    """obtener_cargador() -> el CargadorRespaldo (o None si no existe aun)."""
    r = APIRouter()
    protegido = [Depends(requerir_token_carga)]

    def c():
        cg = obtener_cargador()
        if cg is None:
            raise HTTPException(status_code=503,
                                detail="La carga del respaldo todavia no esta lista.")
        return cg

    def errores(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except mod.NoEncontrado as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except mod.Conflicto as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except mod.ErrorCargaRespaldo as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @r.get("/carga-mof", response_class=HTMLResponse, include_in_schema=False)
    def pagina():
        return HTMLResponse(PAGINA.read_text(encoding="utf-8"))

    @r.get("/api/mof/respaldo/estado", dependencies=protegido)
    def estado():
        return c().estado()

    @r.post("/api/mof/respaldo", dependencies=protegido, status_code=202)
    async def subir(
        archivo: UploadFile = File(..., description="pg_dump de MOF en texto plano (.sql)"),
        simular: str = Form("false", description="true: solo comparar, no tocar nada"),
        permitir_bajas: str = Form("false", description=(
            "true: no detenerse aunque se eliminen muchas unidades o el "
            "respaldo sea mas viejo")),
    ):
        maximo = mod.MAX_MB * 1024 * 1024
        datos = bytearray()
        while True:
            trozo = await archivo.read(1024 * 1024)
            if not trozo:
                break
            datos += trozo
            if len(datos) > maximo:
                raise HTTPException(
                    status_code=413,
                    detail=f"El archivo pasa de {mod.MAX_MB:.0f} MB (MOF_RESPALDO_MAX_MB).")
        return mod.resumen(errores(c().recibir, archivo.filename, bytes(datos),
                                   simular=_bool(simular),
                                   permitir_bajas=_bool(permitir_bajas)))

    @r.get("/api/mof/respaldo/trabajos", dependencies=protegido)
    def trabajos():
        return {"trabajos": c().trabajos()}

    @r.get("/api/mof/respaldo/trabajos/{tid}", dependencies=protegido)
    def trabajo(tid: str):
        return errores(c().trabajo, tid)

    @r.get("/api/mof/respaldo/trabajos/{tid}/eliminado", dependencies=protegido)
    def eliminado(tid: str):
        t = errores(c().trabajo, tid)
        inf = t.get("informe") or {}
        cmp = inf.get("comparacion") or {}
        return {
            "id": tid, "estado": t["estado"],
            "resumen": cmp.get("resumen"),
            "unidades_eliminadas": cmp.get("unidades_eliminadas", []),
            "funciones_eliminadas": cmp.get("funciones_eliminadas", []),
            "cargos_eliminados": cmp.get("cargos_eliminados", []),
            "campos_vaciados": [
                dict({k: m[k] for k in ("id", "nombre", "sigla")}, campo=campo,
                     antes=v["antes"])
                for m in cmp.get("unidades_modificadas", [])
                for campo, v in (m.get("campos") or {}).items()
                if v["antes"] not in (None, "") and v["despues"] in (None, "")],
            "dados_de_baja_en_mof": (inf.get("respaldo") or {}).get("bajas_en_mof"),
        }

    @r.post("/api/mof/respaldo/trabajos/{tid}/confirmar", dependencies=protegido)
    def confirmar(tid: str):
        return mod.resumen(errores(c().confirmar, tid))

    @r.delete("/api/mof/respaldo/trabajos/{tid}", dependencies=protegido)
    def descartar(tid: str):
        return errores(c().descartar, tid)

    return r
