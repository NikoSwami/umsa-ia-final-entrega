# -*- coding: utf-8 -*-
"""
Construye TODA la base de forma AUTOMATICA, sin menus interactivos:

    chunks -> embeddings -> FAISS -> grafo LLM -> adyacencia -> firmantes

Un solo comando hace todo el pipeline de creacion de la base de datos y los
embeddings. Pensado para Colab o para reconstruir tras agregar documentos.

Uso:
    python construir_todo.py                 # rebuild completo (no interactivo)
    python construir_todo.py --incremental   # solo docs nuevos/cambiados
    python construir_todo.py --limit 200     # prueba rapida (200 docs)
    python construir_todo.py --solo-base     # sin el grafo de citas
    python construir_todo.py --sin-grafo-llm # no pasar el LLM por los docs nuevos

Notas:
  - generar_base.py se invoca con --reset (rebuild) o --reanudar (incremental),
    asi NUNCA pregunta nada (apto para automatizacion). --reanudar es la flag
    que de verdad SALTA los docs cuyos chunks ya estan en SQLite; --no-menu
    solo callaba el menu y volvia a chunkear el corpus entero.
  - En modo incremental la Fase B tambien lo es: se reciclan del indice FAISS
    anterior los vectores cuyo texto no cambio y solo se calculan los nuevos.
  - Los documentos que estan en raw/ pero todavia no en grafo_llm.json se
    pasan por el LLM local (extraer_grafo_llm.py, incremental y resumible).
    Ese paso es el unico que necesita GPU: si falla, NO aborta el pipeline —
    avisa y sigue, y al final se reporta que documentos quedaron sin grafo.
  - El grafo usa la adyacencia desde grafo_llm.json (Qwen) si existe; si no,
    cae al grafo regex (construir_grafo_citas.py).
"""
import argparse
import hashlib
import json
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

Paso = namedtuple("Paso", "nombre cmd obligatorio")

def docs_sin_grafo(raiz) -> list:
    """doc_ids que estan en data/raw/ CON texto pero todavia no en
    grafo_llm.json. Son los que no tienen tema, personas ni citas: invisibles
    para el grafo y para los indices de personas/temas aunque si sean
    buscables por texto.

    Los documentos SIN OCR quedan fuera: no tienen contenido que extraer, no
    entran a la base vectorial (generar_base los registra como SIN_OCR) y
    contarlos como pendientes haria que el pipeline pidiera GPU en cada
    corrida para no hacer nada."""
    raiz = Path(raiz)
    carpeta_raw = raiz / "gaceta_rag" / "data" / "raw"
    ruta_grafo = raiz / "gaceta_rag" / "data" / "grafo_llm.json"
    if not carpeta_raw.exists():
        return []

    ya = set()
    if ruta_grafo.exists():
        try:
            with open(ruta_grafo, encoding="utf-8") as f:
                ya = set(json.load(f).get("docs", {}))
        except Exception:
            ya = set()

    faltan = []
    for ruta in sorted(carpeta_raw.glob("doc_*.json")):
        if ruta.stem in ya:
            continue
        if _es_ficha_mof(ruta.stem):
            continue
        if not _contenido_de(ruta).strip():
            continue
        faltan.append(ruta.stem)
    return faltan

def _es_ficha_mof(doc_id) -> bool:
    """True si el doc_id es una ficha del organigrama (MOF), no una resolucion.

    Se replica el rango en vez de importar gaceta_rag.mof.exportador porque
    este modulo tiene que poder correr sin el paquete completo; un test
    verifica que los dos digan lo mismo.

    Importa para el grafo: una ficha no tiene tema, citas ni personas que
    extraer con el LLM (sus enlaces salen deterministas de la base legal, ver
    gaceta_rag/mof/enlaces.py). Sin esta exclusion, agregar el organigrama
    haria que el pipeline pidiera GPU para procesar ~159 documentos que no lo
    necesitan.
    """
    import re as _re
    m = _re.fullmatch(r"doc_(\d+)", str(doc_id or ""))
    return bool(m) and 900_000 < int(m.group(1)) <= 999_999

def docs_sin_ocr(raiz) -> list:
    """doc_ids de raw/ sin texto OCR: no entran ni a la base ni al grafo."""
    raiz = Path(raiz)
    carpeta_raw = raiz / "gaceta_rag" / "data" / "raw"
    if not carpeta_raw.exists():
        return []
    return [p.stem for p in sorted(carpeta_raw.glob("doc_*.json"))
            if not _es_ficha_mof(p.stem) and not _contenido_de(p).strip()]

def _contenido_de(ruta) -> str:
    """OCR crudo de un doc_*.json; "" si falta el campo o el JSON esta roto."""
    try:
        with open(ruta, encoding="utf-8") as f:
            return json.load(f).get("contenido") or ""
    except Exception:
        return ""

def _huella(contenido) -> str:
    """Misma huella que extraer_grafo_llm.doc_hash y generar_base.hash_contenido
    (sha1 del contenido crudo, 12 chars). Se replica aqui para no importar el
    extractor, que arrastra las DLLs de CUDA solo por leer una funcion."""
    return hashlib.sha1((contenido or "").encode("utf-8", "ignore")).hexdigest()[:12]

def docs_con_texto_cambiado(raiz) -> list:
    """doc_ids que SI estan en grafo_llm.json pero cuyo texto crudo cambio
    despues de haberse extraido (p.ej. la reparacion del marcador N° del OCR
    toco casi todo el corpus). Su tema/personas/citas salieron de un texto
    viejo. Re-extraerlos cuesta ~20 s de GPU cada uno, asi que el pipeline NO
    lo hace solo: los reporta para que sea una decision deliberada."""
    raiz = Path(raiz)
    carpeta_raw = raiz / "gaceta_rag" / "data" / "raw"
    ruta_grafo = raiz / "gaceta_rag" / "data" / "grafo_llm.json"
    if not (carpeta_raw.exists() and ruta_grafo.exists()):
        return []
    try:
        with open(ruta_grafo, encoding="utf-8") as f:
            hechos = json.load(f).get("docs", {})
    except Exception:
        return []

    cambiados = []
    for ruta in sorted(carpeta_raw.glob("doc_*.json")):
        previo = hechos.get(ruta.stem)
        if not isinstance(previo, dict) or "_hash" not in previo:
            continue
        if previo["_hash"] != _huella(_contenido_de(ruta)):
            cambiados.append(ruta.stem)
    return cambiados

def plan_pasos(raiz, py, limit=None, incremental=False,
               con_grafo=True, grafo_llm_existe=False,
               pendientes_grafo=0, con_grafo_llm=True):
    """Devuelve [Paso(nombre, cmd, obligatorio), ...]. Funcion PURA (testeable):
    no toca el disco, recibe `grafo_llm_existe` y `pendientes_grafo` ya
    calculados."""
    raiz = Path(raiz)
    mejoras = raiz / "gaceta_rag" / "mejoras"
    pasos = []

    base = [py, str(raiz / "gaceta_rag" / "generar_base.py")]
    base += ["--reanudar"] if incremental else ["--reset"]
    if limit:
        base += ["--limit", str(limit)]
    pasos.append(Paso("Base vectorial (chunks + embeddings + FAISS)", base, True))

    if con_grafo:
        if grafo_llm_existe:
            if con_grafo_llm and pendientes_grafo:
                pasos.append(Paso(
                    f"Grafo LLM: {pendientes_grafo} doc(s) pendiente(s) (necesita GPU)",
                    [py, str(mejoras / "extraer_grafo_llm.py"), "--solo-faltantes"],
                    False,
                ))
            pasos.append(Paso(
                "Identidad del titulo sobre grafo_llm.json",
                [py, str(mejoras / "corregir_identidad_grafo.py")], True))
            pasos.append(Paso(
                "Grafo de citas (adyacencia desde grafo_llm.json)",
                [py, str(mejoras / "construir_adyacencia.py")], True))
            pasos.append(Paso(
                "Firmantes (personas_firmantes.json)",
                [py, str(mejoras / "firmantes.py")], True))
        else:
            pasos.append(Paso(
                "Grafo de citas (regex, sin LLM)",
                [py, str(mejoras / "construir_grafo_citas.py")], True))

        pasos.append(Paso(
            "Enlaces MOF (unidad -> resolucion que la crea)",
            [py, "-m", "gaceta_rag.mof.enlaces"], False))
    return pasos

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Construye TODA la base (chunks, embeddings, FAISS, grafo) automaticamente.")
    ap.add_argument("--limit", type=int, default=None,
                    help="procesar solo N documentos (prueba rapida)")
    ap.add_argument("--incremental", action="store_true",
                    help="solo docs nuevos/cambiados (en vez de rebuild completo)")
    ap.add_argument("--solo-base", action="store_true",
                    help="no construir el grafo de citas")
    ap.add_argument("--sin-grafo-llm", action="store_true",
                    help="no pasar el LLM por los docs que faltan en grafo_llm.json "
                         "(util si no hay GPU; quedan sin tema/personas/citas)")
    args = ap.parse_args(argv)

    raiz = Path(__file__).resolve().parent
    grafo_llm = raiz / "gaceta_rag" / "data" / "grafo_llm.json"
    pendientes = docs_sin_grafo(raiz) if not args.solo_base else []

    pasos = plan_pasos(raiz, sys.executable, limit=args.limit,
                       incremental=args.incremental, con_grafo=not args.solo_base,
                       grafo_llm_existe=grafo_llm.exists(),
                       pendientes_grafo=len(pendientes),
                       con_grafo_llm=not args.sin_grafo_llm)

    print(f"Pipeline automatico: {len(pasos)} paso(s).")
    if pendientes:
        muestra = ", ".join(pendientes[:5]) + ("..." if len(pendientes) > 5 else "")
        print(f"  Documentos sin grafo LLM: {len(pendientes)} ({muestra})")
    sin_ocr = docs_sin_ocr(raiz)
    if sin_ocr:
        muestra = ", ".join(sin_ocr[:5]) + ("..." if len(sin_ocr) > 5 else "")
        print(f"  Documentos sin OCR (no entran a la base ni al grafo): "
              f"{len(sin_ocr)} ({muestra})")

    fallidos = []
    for i, paso in enumerate(pasos, 1):
        print(f"\n=== [{i}/{len(pasos)}] {paso.nombre} ===", flush=True)
        r = subprocess.run(paso.cmd, cwd=str(raiz))
        if r.returncode == 0:
            continue
        if paso.obligatorio:
            print(f"  [ERROR] el paso fallo (codigo {r.returncode}). Abortando.")
            return r.returncode
        fallidos.append(paso.nombre)
        print(f"  [AVISO] el paso fallo (codigo {r.returncode}) pero no es "
              f"obligatorio; el pipeline sigue.")

    quedan = docs_sin_grafo(raiz) if not args.solo_base else []
    viejos = docs_con_texto_cambiado(raiz) if not args.solo_base else []
    if viejos:
        print(f"\nAviso: {len(viejos)} doc(s) tienen el grafo extraido de un texto")
        print( "  anterior al que hay hoy en raw/. Siguen funcionando, pero su")
        print( "  tema/personas/citas salieron de la version vieja. Re-extraerlos")
        print( "  es deliberado (~20 s de GPU por doc):")
        print( "    python gaceta_rag/mejoras/extraer_grafo_llm.py")
    if fallidos or quedan:
        print("\nTERMINADO CON PENDIENTES:")
        for nombre in fallidos:
            print(f"  - fallo (no obligatorio): {nombre}")
        if quedan:
            muestra = ", ".join(quedan[:5]) + ("..." if len(quedan) > 5 else "")
            print(f"  - {len(quedan)} doc(s) sin grafo LLM: {muestra}")
            print(f"    son buscables por texto, pero sin tema, personas ni citas.")
            print(f"    corregilo con: python gaceta_rag/mejoras/extraer_grafo_llm.py")
        return 0

    print("\nLISTO: base de datos + embeddings + grafo construidos automaticamente.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
