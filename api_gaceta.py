"""
API GACETA IA - Backend FastAPI (UNIFICADA)
============================================
Expone el motor RAG (MotorGaceta) como una API HTTP y sirve la interfaz
React compilada (frontend/dist).

Esta es la API UNICA del proyecto, con TODAS las funciones: chat en
streaming, interruptor de listas y branding FCPN.

Endpoints:
  GET  /api/status   -> estado del motor + estado de listas + branding
  POST /api/nueva    -> limpia el historial conversacional del motor
  POST /api/shutdown -> termina el proceso del servidor (boton "Cerrar Sesión")
  GET  /api/listas   -> estado del interruptor de "consulta a las listas"
  POST /api/listas   -> activa/desactiva la consulta a listas en caliente
  GET  /api/branding -> indica al frontend que use el modo visual FCPN
  POST /api/chat     -> respuesta del modelo en streaming (Server-Sent Events)
  GET  /api/ingesta/mof            -> estado de la ingesta del organigrama
  POST /api/ingesta/mof            -> aplica un evento de MOF en caliente
  POST /api/ingesta/mof/persistir  -> baja a disco lo ingerido en memoria
  POST /api/reconciliar/mof        -> compara MOF con lo indexado y lo arregla
  GET  /api/mof/organigrama        -> consultas EXACTAS sobre la replica local
  GET  /carga + /api/carga/...     -> carga manual de documentos (api_carga.py)
  GET  /carga-mof + /api/mof/respaldo/... -> carga del respaldo (pg_dump) de MOF
                                     con informe de lo eliminado (api_mof_respaldo.py)
  GET  /             -> sirve la interfaz React compilada (frontend/dist)

Los POST de control (nueva, shutdown, listas, memoria) se pueden proteger
con la variable de entorno GACETA_ADMIN_TOKEN (ver mas abajo, seccion
"TOKEN DE ADMINISTRADOR"). Util cuando la app se expone por un tunel
publico (cloudflared en Colab): sin la variable, todo funciona igual que
siempre.

El modelo (Qwen2.5-7B + RAG) se carga en un hilo en segundo plano al
arrancar para no bloquear el servidor.
"""

import argparse
import contextlib
import json
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TOP_K_FAISS", "30")
os.environ.setdefault("TOP_K_FTS", "20")
os.environ.setdefault("TOP_K_FINAL", "10")
os.environ.setdefault("USAR_RERANKER", "1")

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

import api_carga
import api_mof_respaldo
import api_paperless
import api_mcp_guia
from gaceta_rag.paperless import cliente as paperless_cliente
from gaceta_rag.paperless import puente as paperless_puente
from gaceta_rag.paperless import reconciliar as paperless_reconciliar
import motor_web
from gaceta_rag.carga import cargador as carga_cargador
from gaceta_rag.carga import ocr as carga_ocr
from gaceta_rag.mof import carga_respaldo as mof_carga_respaldo
from gaceta_rag.mof import cliente as mof_cliente
from gaceta_rag.mof import ingesta as mof_ingesta
from gaceta_rag.mof import novedades as mof_novedades
from gaceta_rag.mof import reconciliar as mof_reconciliar

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

DIST_DIR = BASE_DIR / "frontend" / "dist"

def _requerir_admin(x_admin_token: str | None = Header(default=None)):
    token = os.environ.get("GACETA_ADMIN_TOKEN")
    if token and x_admin_token != token:
        raise HTTPException(
            status_code=401,
            detail="Token de administrador invalido o faltante (header X-Admin-Token).",
        )

def _requerir_token_ingesta(x_admin_token: str | None = Header(default=None)):
    validos = {
        os.environ.get("GACETA_INGESTA_TOKEN"),
        os.environ.get("GACETA_ADMIN_TOKEN"),
    }
    validos.discard(None)
    validos.discard("")

    if not validos:
        raise HTTPException(
            status_code=503,
            detail=(
                "La ingesta de MOF esta deshabilitada: no hay ningun token "
                "definido. Defini GACETA_INGESTA_TOKEN (recomendado) o "
                "GACETA_ADMIN_TOKEN, y el mismo valor en el .env de MOF."
            ),
        )
    if x_admin_token not in validos:
        raise HTTPException(
            status_code=401,
            detail="Token invalido o faltante (header X-Admin-Token).",
        )

BRANDING = {
    "mode": "fcpn",
    "colors": {
        "azul_umsa": "#0B2D5C",
        "rojo_institucional": "#A32035",
    },
}

def _env_bool(nombre: str, por_defecto: bool) -> bool:
    valor = os.environ.get(nombre)
    if valor is None:
        return por_defecto
    return valor.strip().lower() in ("1", "true", "yes", "on", "si", "sí")

_state = {
    "listas_habilitadas": _env_bool("LISTAS_HABILITADAS", True),
    "memoria_habilitada": _env_bool("MEMORIA_HABILITADA", True),
}

_motor: motor_web.MotorGaceta = motor_web.MotorGaceta(
    estado_mof=lambda: _consultor.estado(cursor=False))

def _cargar_motor():
    try:
        _motor.cargar()
    except Exception:
        import traceback
        traceback.print_exc()

def _segundos(nombre: str, por_defecto: float) -> float:
    valor = os.environ.get(nombre)
    if valor is None or not str(valor).strip():
        return por_defecto
    try:
        return max(0.0, float(valor))
    except ValueError:
        return por_defecto

def _avisar_si_faltan_credenciales():
    """MOF pide sesion tambien para leer el organigrama: con MOF_URL y sin
    cuenta, la consulta automatica va a fallar en cada vuelta. Se dice una
    vez, al arrancar, en vez de dejar que se descubra por el primer 401."""
    aviso = _cliente_mof.aviso_sin_credenciales
    if aviso:
        print(f"[mof] AVISO: {aviso}")

def _bucle_mof():
    if not _cliente_mof.configurado:
        return
    _avisar_si_faltan_credenciales()
    cada = _segundos("MOF_CONSULTAR_CADA_S", 30)
    cada_reconciliar = _segundos("MOF_RECONCILIAR_CADA_S", 900)
    cada_profunda = int(_segundos("MOF_RECONCILIAR_PROFUNDA", 12))
    if cada <= 0 and cada_reconciliar <= 0:
        return

    while not _ingestor.disponible:
        time.sleep(2)

    proxima_reconciliacion = 0.0
    vuelta = 0
    aviso_anterior = None
    while True:
        if _env_bool("USAR_INGESTA_MOF", True):
            if cada > 0:
                r = _consultor.consultar()
                aviso_nuevo = r.get("aviso") and r.get("aviso") != aviso_anterior
                aviso_anterior = r.get("aviso")
                if (r.get("aplicadas") or r.get("bajas") or aviso_nuevo
                        or r.get("errores") or r.get("tipo") == "carga_completa"):
                    print(f"[mof] {r.get('tipo', 'consulta')} "
                          f"({r.get('modo', '?')}): {r.get('aplicadas', 0)} "
                          f"aplicadas, {r.get('reindexadas', 0)} reindexadas, "
                          f"{r.get('bajas', 0)} bajas"
                          + (f" | AVISO: {r['aviso']}" if aviso_nuevo else ""))

            if cada_reconciliar > 0 and time.time() >= proxima_reconciliacion:
                vuelta += 1
                profundo = bool(cada_profunda) and vuelta % cada_profunda == 0
                try:
                    r = _reconciliador.ejecutar(profundo=profundo)
                except Exception as exc:
                    r = {"ok": False, "motivo": str(exc)}
                proxima_reconciliacion = time.time() + cada_reconciliar
                plan = r.get("plan") or {}
                if r.get("aplicadas") or r.get("bajas") or plan.get("aviso"):
                    print(f"[mof] reconciliacion "
                          f"{'profunda' if profundo else 'rapida'}: "
                          f"{r.get('aplicadas', 0)} aplicadas "
                          f"({r.get('reindexadas', 0)} reindexadas), "
                          f"{r.get('bajas', 0)} bajas"
                          + (f" | AVISO: {plan['aviso']}"
                             if plan.get("aviso") else ""))

            try:
                _ingestor.persistir_si_hace_falta()
            except Exception as exc:
                print(f"[mof] no se pudo bajar el indice a disco: {exc}")

        time.sleep(cada if cada > 0 else cada_reconciliar)

def _bucle_paperless():
    """La red para lo que el webhook de Paperless no ve (reconciliar.py)."""
    cada = _segundos("PAPERLESS_RECONCILIAR_CADA_S", 300)
    if not _puente.cliente.configurado or cada <= 0:
        return
    while not _cargador.listo:
        time.sleep(2)
    while True:
        try:
            r = _reconciliador_paperless.ejecutar()
            if (r["altas"] or r["cambios"] or r["bajas"] or r["aviso"]
                    or r["errores"] or not r["ok"]):
                print(f"[paperless] reconciliacion: {len(r['altas'])} altas, "
                      f"{len(r['cambios'])} cambios, {len(r['bajas'])} bajas, "
                      f"{len(r['errores'])} errores"
                      + (f" | {r['motivo']}" if r["motivo"] else "")
                      + (f" | AVISO: {r['aviso']}" if r["aviso"] else ""))
        except Exception as exc:
            print(f"[paperless] reconciliacion: {type(exc).__name__}: {exc}")
        time.sleep(cada)

@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_cargar_motor, daemon=True).start()
    threading.Thread(target=_bucle_mof, daemon=True).start()
    threading.Thread(target=_bucle_paperless, daemon=True).start()
    _cargador.iniciar()
    _puente.iniciar()
    yield

app = FastAPI(title="Consulta a la Gaceta IA - API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/status")
def status():
    """Estado de carga del motor de IA + estado de listas + branding."""
    return {
        "loading": _motor.loading,
        "loaded": _motor.loaded,
        "error": _motor.error,
        "listas_habilitadas": _state["listas_habilitadas"],
        "memoria_habilitada": _state["memoria_habilitada"],
        "branding": BRANDING["mode"],
    }

@app.post("/api/nueva", dependencies=[Depends(_requerir_admin)])
def nueva_conversacion():
    """Limpia el historial conversacional del motor."""
    _motor.limpiar_historial()
    return {"ok": True}

@app.post("/api/shutdown", dependencies=[Depends(_requerir_admin)])
def shutdown():
    """Termina el proceso del servidor. Lo invoca el boton 'Cerrar Sesión'
    de la interfaz: corta el modelo, libera el puerto y deja la ventana
    del navegador sin conexion."""
    def matar():
        time.sleep(0.4)
        os._exit(0)
    threading.Thread(target=matar, daemon=True).start()
    return {"ok": True, "mensaje": "Servidor cerrándose..."}

@app.get("/api/listas")
def listas_get():
    """Estado actual del interruptor de consulta a listas."""
    return {"habilitadas": _state["listas_habilitadas"]}

@app.post("/api/listas", dependencies=[Depends(_requerir_admin)])
async def listas_set(request: Request):
    """Activa o desactiva la consulta a listas en caliente."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": "Body JSON invalido. Espera {\"habilitadas\": true|false}."},
            status_code=400,
        )
    if "habilitadas" not in body:
        return JSONResponse(
            {"error": "Falta el campo 'habilitadas' (true|false)."},
            status_code=400,
        )
    _state["listas_habilitadas"] = bool(body["habilitadas"])
    print(f"[interruptor] listas_habilitadas = {_state['listas_habilitadas']}")
    return {"habilitadas": _state["listas_habilitadas"]}

@app.get("/api/memoria")
def memoria_get():
    """Estado actual del interruptor de 'memoria' (contexto de la conversacion)."""
    return {"habilitada": _state["memoria_habilitada"]}

@app.post("/api/memoria", dependencies=[Depends(_requerir_admin)])
async def memoria_set(request: Request):
    """Activa o desactiva que el motor recuerde el contexto de la conversacion."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": "Body JSON invalido. Espera {\"habilitada\": true|false}."},
            status_code=400,
        )
    if "habilitada" not in body:
        return JSONResponse(
            {"error": "Falta el campo 'habilitada' (true|false)."},
            status_code=400,
        )
    _state["memoria_habilitada"] = bool(body["habilitada"])
    print(f"[interruptor] memoria_habilitada = {_state['memoria_habilitada']}")
    return {"habilitada": _state["memoria_habilitada"]}

_ingestor = mof_ingesta.Ingestor(motor=_motor)
_motor.ruta_replica = _ingestor.ruta_replica
_cliente_mof = mof_cliente.ClienteMOF()
_reconciliador = mof_reconciliar.Reconciliador(
    cliente=_cliente_mof, ingestor=_ingestor)
_consultor = mof_novedades.Consultor(_cliente_mof, _ingestor, _reconciliador)

_cargador = carga_cargador.Cargador(
    motor=_motor, ingestor=_ingestor,
    motor_ocr=carga_ocr.motor_desde_entorno())
app.include_router(api_carga.crear_router(lambda: _cargador))

_puente = paperless_puente.Puente(paperless_cliente.ClientePaperless(), _cargador)
_reconciliador_paperless = paperless_reconciliar.Reconciliador(_puente)
app.include_router(api_paperless.crear_router(
    lambda: _puente, lambda: _reconciliador_paperless))
app.include_router(api_mcp_guia.crear_router(lambda: _puente))

@contextlib.contextmanager
def _candado_indice():
    with _cargador._lock_indice, _cargador._candado_motor():
        yield

_cargador_respaldo = mof_carga_respaldo.CargadorRespaldo(
    ingestor=_ingestor, carpeta=_ingestor.ruta_sqlite.parent / "cargas_mof",
    candado=_candado_indice)
app.include_router(api_mof_respaldo.crear_router(lambda: _cargador_respaldo))

@app.get("/api/ingesta/mof")
def ingesta_mof_estado():
    """Estado de la ingesta: si el motor esta listo y cuanto falta persistir."""
    estado = _ingestor.estado()
    estado["habilitada"] = _env_bool("USAR_INGESTA_MOF", True)
    estado["mof_configurado"] = _cliente_mof.configurado
    estado["consulta"] = _consultor.estado()
    return estado

@app.post("/api/ingesta/mof", dependencies=[Depends(_requerir_token_ingesta)])
async def ingesta_mof(request: Request):
    """Aplica UN evento de MOF.

    Body: {"evento_id": "...", "accion": "upsert"|"delete", "unidad_id": 12}
    Opcional: "unidad", "funciones", "personal", "padre" para no ir a buscarlos.

    Codigos: 200 aplicado (o repetido), 400 evento mal formado,
    503 motor cargando o MOF inalcanzable -> MOF debe REINTENTAR.
    """
    if not _env_bool("USAR_INGESTA_MOF", True):
        return JSONResponse({"error": "Ingesta de MOF desactivada."},
                            status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Body JSON invalido."}, status_code=400)

    accion = str(body.get("accion") or "upsert").lower()
    unidad_id = body.get("unidad_id")
    if unidad_id is None and isinstance(body.get("unidad"), dict):
        unidad_id = body["unidad"].get("id")
    if unidad_id is None:
        return JSONResponse({"error": "Falta 'unidad_id'."}, status_code=400)

    unidad = body.get("unidad")
    funciones = body.get("funciones")
    personal = body.get("personal")
    padre = body.get("padre")

    if accion == "upsert" and not isinstance(unidad, dict):
        if not _cliente_mof.configurado:
            return JSONResponse(
                {"error": "MOF_URL no configurado y el evento no trae 'unidad'."},
                status_code=400)
        try:
            datos = _cliente_mof.unidad_completa(unidad_id)
        except mof_cliente.ErrorMOF as exc:
            return JSONResponse({"error": f"MOF inalcanzable: {exc}"},
                                status_code=503)
        if not datos:
            accion = "delete"
        else:
            unidad = datos["unidad"]
            funciones = datos["funciones"]
            personal = datos["personal"]
            padre = datos["padre"]

    try:
        resultado = _ingestor.aplicar(
            accion=accion, unidad=unidad, funciones=funciones,
            personal=personal, padre=padre, unidad_id=unidad_id,
            evento_id=body.get("evento_id"),
            facultad=body.get("facultad"),
        )
    except mof_ingesta.MotorNoListo as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    return dict(resultado)

@app.post("/api/reconciliar/mof", dependencies=[Depends(_requerir_token_ingesta)])
async def reconciliar_mof(request: Request):
    """Compara MOF con lo indexado y arregla la diferencia.

    Body (todo opcional):
      {"profundo": false, "dry_run": false, "forzar": false}

    profundo : revisa cada unidad entera. Es la unica pasada que ve un cambio
               en una FUNCION o en el personal, porque esos no asoman en el
               listado de MOF. Cuesta ~3 peticiones por unidad.
    dry_run  : dice que haria y no toca nada.
    forzar   : aplica las bajas aunque salte la valvula de seguridad.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    resultado = _reconciliador.ejecutar(
        profundo=bool(body.get("profundo")),
        dry_run=bool(body.get("dry_run")),
        forzar=bool(body.get("forzar")),
    )
    if not resultado.get("ok"):
        return JSONResponse(resultado, status_code=503)
    return resultado

@app.post("/api/ingesta/mof/persistir", dependencies=[Depends(_requerir_token_ingesta)])
def ingesta_mof_persistir():
    """Baja a disco el indice y la metadata que la ingesta tiene en memoria.

    La ingesta ya lo hace sola cada MOF_FLUSH_CADA eventos o cada
    MOF_FLUSH_SEGUNDOS; esto es para forzarlo antes de apagar."""
    return _ingestor.persistir()

@app.get("/api/mof/organigrama")
def mof_organigrama(unidad: int = None, sigla: str = None,
                    buscar: str = None, rama: bool = False):
    """Consultas EXACTAS sobre la replica local del organigrama.

    Esto no es la busqueda del chat: no entiende preguntas, no ordena por
    parecido y no inventa. Cuenta y lista. Es lo que permite responder
    "cuantas unidades dependen de X" con un numero y no con una aproximacion.

    Solo LECTURA, y solo de datos que MOF ya publica sin autenticacion en su
    propio listado: no expone nada nuevo, ni deja escribir nada.

      (sin parametros)  -> totales, relaciones y tipos
      ?unidad=44        -> la unidad, sus funciones, sus cargos y sus hijas
      ?unidad=44&rama=1 -> ademas, toda la rama debajo
      ?sigla=IIQ        -> lo mismo, buscando por sigla
      ?buscar=quimica   -> unidades cuyo nombre o sigla coincida
    """
    from gaceta_rag.mof import replica as mof_replica

    try:
        conn = mof_replica.abrir_lectura(_ingestor.ruta_replica)
    except FileNotFoundError:
        return JSONResponse(
            {"disponible": False,
             "detalle": "La replica todavia no existe. Se crea sola la primera "
                        "vez que la Gaceta consulta a MOF: ver "
                        "GET /api/ingesta/mof."},
            status_code=503)
    except Exception as exc:
        return JSONResponse(
            {"disponible": False, "error": str(exc)}, status_code=503)
    try:
        return _organigrama(conn, mof_replica, unidad, sigla, buscar, rama)
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse(
            {"disponible": False, "error": str(exc)}, status_code=503)
    finally:
        conn.close()

def _organigrama(conn, mof_replica, unidad, sigla, buscar, rama):
    def filas(rs):
        return [dict(r) for r in rs]

    total = mof_replica.contar_unidades(conn)
    if not total:
        return JSONResponse(
            {"disponible": False,
             "detalle": "La replica esta vacia. Se llena sola con la primera "
                        "consulta a MOF: ver GET /api/ingesta/mof."},
            status_code=503)

    if buscar:
        return {"disponible": True, "coincidencias": filas(
            mof_replica.buscar(conn, buscar))}

    if sigla and unidad is None:
        encontrada = mof_replica.por_sigla(conn, sigla)
        if encontrada is None:
            candidatas = mof_replica.buscar(conn, sigla)
            if not candidatas:
                raise HTTPException(status_code=404,
                                    detail=f"No hay ninguna unidad {sigla!r}.")
            encontrada = candidatas[0]
        unidad = encontrada["id"]

    if unidad is not None:
        u = mof_replica.unidad(conn, unidad)
        if u is None:
            raise HTTPException(status_code=404,
                                detail=f"La unidad {unidad} no esta en la replica.")
        hijas = mof_replica.hijas_de(conn, unidad)
        salida = {
            "disponible": True,
            "unidad": dict(u),
            "funciones": filas(mof_replica.funciones_de(conn, unidad)),
            "cargos": filas(mof_replica.cargos_de(conn, unidad)),
            "dependen_directamente": len(hijas),
            "hijas": filas(hijas),
        }
        if rama:
            debajo = mof_replica.descendientes_de(conn, unidad)
            salida["en_toda_la_rama"] = len(debajo)
            salida["rama"] = filas(debajo)
        return salida

    return {
        "disponible": True,
        "resumen": mof_replica.resumen(conn),
        "raices": filas(mof_replica.raices(conn)),
        "por_relacion": filas(mof_replica.relaciones(conn)),
        "por_tipo": filas(mof_replica.tipos(conn)),
    }

@app.get("/api/branding")
def branding():
    """Le indica al frontend que use el modo visual FCPN."""
    return BRANDING

_sugerencias_cache = {"pool": None}

def _pool_sugerencias():
    if _sugerencias_cache["pool"] is not None:
        return _sugerencias_cache["pool"]
    pool = []
    try:
        base = Path(__file__).parent / "gaceta_rag" / "data"
        with open(base / "grafo_llm.json", encoding="utf-8") as f:
            llm = json.load(f)["docs"]
        with open(base / "grafo_citas.json", encoding="utf-8") as f:
            citas = json.load(f)["docs"]

        def etq(did):
            v = citas.get(did, {})
            o, n, a = v.get("org"), v.get("num"), v.get("anio")
            return f"{o} {n}/{a}" if o and n and a else None

        vistos = set()
        for did in sorted(llm):
            tema = str(llm[did].get("tema") or "").strip()
            tnorm = tema.lower()
            if 25 <= len(tema) <= 75 and tnorm not in vistos:
                vistos.add(tnorm)
                pool.append(f"¿Qué dice la resolución sobre {tnorm}?")
                e = etq(did)
                if e:
                    pool.append(f"Resume la Resolución {e}")
                    pool.append(f"¿Sigue vigente la Resolución {e}?")
    except Exception as e:
        print(f"[sugerencias] sin pool del corpus: {e}")
        pool = ["¿Qué dice el reglamento de titulación vigente?",
                "¿Cuál es el calendario académico aprobado?"]
    _sugerencias_cache["pool"] = pool
    return pool

@app.get("/api/sugerencias")
def sugerencias(n: int = 4):
    """N preguntas de ejemplo tomadas del CORPUS (rotan en cada llamada)."""
    import random
    pool = _pool_sugerencias()
    n = max(1, min(int(n), 8))
    return {"sugerencias": random.sample(pool, min(n, len(pool)))}

@app.post("/api/chat")
async def chat(request: Request):
    """
    Recibe {"message": "..."} y devuelve la respuesta en streaming usando
    Server-Sent Events. Cada evento es un JSON:
      {"type": "chunk", "content": "<texto acumulado>"}
      {"type": "done"}
      {"type": "error", "content": "<mensaje>"}
    """
    body = await request.json()
    mensaje = (body.get("message") or "").strip()

    def sse(obj: dict) -> str:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    def event_stream():
        if not mensaje:
            yield sse({"type": "error", "content": "El mensaje está vacío."})
            return

        espera = 0
        while _motor.loading and espera < 180:
            time.sleep(1)
            espera += 1

        if _motor.error:
            yield sse({"type": "error", "content": _motor.error})
            return
        if not _motor.loaded:
            yield sse({
                "type": "error",
                "content": "El motor de IA aún se está cargando. "
                           "Espera unos segundos e inténtalo de nuevo.",
            })
            return

        try:
            for parcial in _motor.procesar_stream(
                mensaje,
                permitir_listado=_state["listas_habilitadas"],
                usar_memoria=_state["memoria_habilitada"],
            ):
                yield sse({"type": "chunk", "content": parcial})
            yield sse({"type": "done"})
        except Exception as exc:
            import traceback
            traceback.print_exc()
            yield sse({
                "type": "error",
                "content": f"Error interno: {type(exc).__name__}: {exc}",
            })

    return StreamingResponse(event_stream(), media_type="text/event-stream")

_INDEX = DIST_DIR / "index.html"
_ASSETS = DIST_DIR / "assets"
if _INDEX.exists() and _ASSETS.is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=str(_ASSETS)),
        name="assets",
    )

    @app.get("/")
    def index():
        return FileResponse(str(_INDEX))
else:
    @app.get("/")
    def index_sin_build():
        return JSONResponse(
            {
                "mensaje": "El frontend todavía no está compilado.",
                "solucion": "Ejecuta 'iniciar_web_react.bat', o dentro de la "
                            "carpeta 'frontend' corre 'npm install' y "
                            "'npm run build'.",
            },
            status_code=200,
        )

def _abrir_navegador(url: str):
    """Abre el navegador por defecto en `url`. En Windows usa os.startfile
    (lo mas fiable, equivale al comando 'start'); si falla, prueba con
    webbrowser. Cualquier error se reporta pero NO tumba el servidor."""
    try:
        if hasattr(os, "startfile"):
            os.startfile(url)
            print(f"  [navegador] abierto en {url}")
            return
    except Exception as exc:
        print(f"  [navegador] os.startfile fallo ({exc}); probando webbrowser...")
    try:
        import webbrowser
        if webbrowser.open(url):
            print(f"  [navegador] abierto en {url}")
        else:
            print(f"  [navegador] no se pudo abrir solo. Abre manualmente: {url}")
    except Exception as exc:
        print(f"  [navegador] error ({exc}). Abre manualmente: {url}")

def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="API Web - Gaceta UMSA IA")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true",
                        help="No abrir el navegador automaticamente al arrancar.")

    grupo = parser.add_mutually_exclusive_group()
    grupo.add_argument("--no-listas", action="store_true",
                       help="Arrancar con la funcion de listas DESACTIVADA.")
    grupo.add_argument("--listas", action="store_true",
                       help="Arrancar con la funcion de listas ACTIVADA.")

    args = parser.parse_args()

    if args.no_listas:
        _state["listas_habilitadas"] = False
    elif args.listas:
        _state["listas_habilitadas"] = True

    estado_listas = "ACTIVADA" if _state["listas_habilitadas"] else "DESACTIVADA"

    print("=" * 70)
    print("  CONSULTA A LA GACETA IA - API (FastAPI + React)")
    print(f"  Servidor          : http://{args.host}:{args.port}")
    print(f"  Funcion de listas : {estado_listas}")
    print(f"  Branding          : {BRANDING['mode'].upper()} "
          f"(logo FCPN + borde giratorio bicolor)")
    if DIST_DIR.exists():
        print("  Frontend          : frontend/dist (compilado)")
    else:
        print("  Frontend          : SIN COMPILAR - corre 'npm run build' en frontend/")
    print("  El modelo de IA se carga en segundo plano...")
    print("=" * 70)

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        if not os.environ.get("GACETA_ADMIN_TOKEN"):
            print("=" * 70)
            print("  [ATENCION] Escuchando en " + args.host + " SIN GACETA_ADMIN_TOKEN.")
            print("  Los POST de control (shutdown, listas, memoria) quedan")
            print("  ABIERTOS a cualquiera que alcance el puerto, y la ingesta")
            print("  de MOF responde 503 hasta que definas la variable.")
            print("  Defini GACETA_ADMIN_TOKEN con el mismo valor aqui y en el")
            print("  .env de MOF antes de exponerlo.")
            print("=" * 70)
        else:
            print(f"  Escuchando en {args.host} con token de administrador definido.")

    if not args.no_browser:
        host_url = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
        url = f"http://{host_url}:{args.port}"
        threading.Timer(2.0, lambda: _abrir_navegador(url)).start()

    uvicorn.run(app, host=args.host, port=args.port)

if __name__ == "__main__":
    main()
