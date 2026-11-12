"""Corrige el marcador de numero de resolucion ("N°") mal leido por el OCR
de origen en los datos YA cargados: gaceta_rag/data/raw/*.json y
gaceta_rag/data/chunks_v2.sqlite.

El texto extraido confunde el simbolo "N°"/"Nº" con un caracter suelto que
no le corresponde segun el documento: "N*", "N'", "N?", "N<replacement>",
o incluso una letra ("NC", "NY", "NS", "Ne"...). La logica de deteccion
vive en gaceta_rag.generar_base.arreglar_marcador_resolucion (misma
funcion que ahora corre en la ingesta para que un rebuild no reintroduzca
el problema); este script aplica esa MISMA correccion a lo que ya esta
en el repo, sin re-generar embeddings (el simbolo no afecta el significado
semantico del chunk, solo su forma legible).

No toca 'No'/'Nro' (abreviaturas validas de "Numero") ni el numero de
resolucion en si — ver el docstring de arreglar_marcador_resolucion.

Uso:
    python scripts/reparar_marcador_resolucion.py            # aplica
    python scripts/reparar_marcador_resolucion.py --dry-run  # solo cuenta
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from gaceta_rag.generar_base import arreglar_marcador_resolucion

RUTA_RAW = RAIZ / "gaceta_rag" / "data" / "raw"
RUTA_SQLITE = RAIZ / "gaceta_rag" / "data" / "chunks_v2.sqlite"

CAMPOS_JSON_A_REVISAR = ("contenido", "titulo", "tipo_documento")

def reparar_raw(dry_run: bool) -> tuple[int, int]:
    """Aplica el fix al texto crudo de cada doc_NNNNN.json, en el archivo
    completo (no solo al campo 'contenido'): el patron es lo bastante
    especifico (requiere 'resoluci[oó]n N<roto> NNN/AAAA' literal) como
    para no tocar nada fuera de los campos de texto del documento, y asi
    evitamos re-serializar el JSON (se preserva bytes, indentado y CRLF
    tal cual estaban)."""
    archivos = sorted(RUTA_RAW.glob("doc_*.json"))
    n_cambiados = 0
    n_con_error = 0
    for ruta in archivos:
        original = ruta.read_text(encoding="utf-8", newline="")
        corregido = arreglar_marcador_resolucion(original)
        if corregido == original:
            continue

        try:
            doc_antes = json.loads(original)
            doc_despues = json.loads(corregido)
        except json.JSONDecodeError as e:
            print(f"  [SKIP] {ruta.name}: el fix rompio el JSON ({e}); no se escribe")
            n_con_error += 1
            continue

        claves_distintas = {
            k for k in doc_antes.keys() | doc_despues.keys()
            if doc_antes.get(k) != doc_despues.get(k)
        }
        inesperadas = claves_distintas - set(CAMPOS_JSON_A_REVISAR)
        if inesperadas:
            print(f"  [SKIP] {ruta.name}: cambio en campos inesperados {inesperadas}; no se escribe")
            n_con_error += 1
            continue

        n_cambiados += 1
        if not dry_run:
            ruta.write_text(corregido, encoding="utf-8", newline="")

    return n_cambiados, n_con_error

def reparar_chunks_sqlite(dry_run: bool) -> tuple[int, int]:
    conn = sqlite3.connect(str(RUTA_SQLITE))
    n_chunks = 0
    n_meta = 0
    try:
        filas = conn.execute("SELECT rowid, texto FROM chunks").fetchall()
        for rowid, texto in filas:
            corregido = arreglar_marcador_resolucion(texto or "")
            if corregido != texto:
                n_chunks += 1
                if not dry_run:
                    conn.execute(
                        "UPDATE chunks SET texto = ? WHERE rowid = ?",
                        (corregido, rowid),
                    )

        filas_meta = conn.execute(
            "SELECT chunk_id, texto_emb FROM chunks_meta"
        ).fetchall()
        for chunk_id, texto_emb in filas_meta:
            corregido = arreglar_marcador_resolucion(texto_emb or "")
            if corregido != texto_emb:
                n_meta += 1
                if not dry_run:
                    conn.execute(
                        "UPDATE chunks_meta SET texto_emb = ? WHERE chunk_id = ?",
                        (corregido, chunk_id),
                    )

        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return n_chunks, n_meta

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                     help="Solo cuenta cuantos registros cambiarian, sin escribir")
    args = ap.parse_args()

    print("Reparando gaceta_rag/data/raw/*.json ...")
    n_raw, n_err = reparar_raw(args.dry_run)
    print(f"  {n_raw} archivos {'a corregir' if args.dry_run else 'corregidos'}"
          f"{f', {n_err} con error (no tocados)' if n_err else ''}")

    if not RUTA_SQLITE.exists():
        print(f"\n{RUTA_SQLITE} no existe; se omite la reparacion de chunks_v2.sqlite")
        return

    print("\nReparando gaceta_rag/data/chunks_v2.sqlite ...")
    n_chunks, n_meta = reparar_chunks_sqlite(args.dry_run)
    print(f"  {n_chunks} filas de 'chunks' {'a corregir' if args.dry_run else 'corregidas'}")
    print(f"  {n_meta} filas de 'chunks_meta' {'a corregir' if args.dry_run else 'corregidas'}")

    if args.dry_run:
        print("\n(dry-run: no se escribio nada)")

if __name__ == "__main__":
    main()
