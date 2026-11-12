"""Verifica que un directorio no contenga datos sensibles antes de publicarlo.

Uso: python scripts/verificar_publicacion.py <directorio>

Termina con exit 1 si encuentra:
- Patrones de C.I. boliviano (6-8 dígitos seguidos de extensión de departamento)
- Tokens de API (ghp_, gho_, hf_, sk-, etc.)
- Rastros de gaceta_rag/data/raw (rutas o nombres de archivo doc_NNNNN.json)

Ver PRIVACIDAD.md para el contexto completo.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

CI_BOLIVIANO_RE = re.compile(
    r"\bC\.?I\.?\s*\d{6,8}\s*[-]?\s*(?:LP|CB|SC|OR|PT|CH|TJ|BE|PD)\b",
    re.IGNORECASE,
)

TOKEN_RE = re.compile(
    r"\b(?:ghp_|gho_|ghu_|ghs_|ghr_|hf_|sk-)[A-Za-z0-9]{10,}\b"
)

RASTRO_DATA_RAW_RE = re.compile(
    r"gaceta_rag[/\\]data[/\\]raw|\bdoc_\d{5}\.json\b"
)

PATRONES = {
    "C.I. boliviano": CI_BOLIVIANO_RE,
    "token de API": TOKEN_RE,
    "rastro de gaceta_rag/data/raw": RASTRO_DATA_RAW_RE,
}

EXTENSIONES_TEXTO = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".md", ".txt", ".yml",
    ".yaml", ".toml", ".cfg", ".ini", ".html", ".css", ".sh", ".bat",
    ".ipynb", ".sql",
}

def escanear_archivo(path: Path) -> list[tuple[str, str]]:
    """Devuelve lista de (nombre_patron, fragmento) encontrados en el archivo."""
    hallazgos: list[tuple[str, str]] = []

    ruta_str = str(path)
    m = RASTRO_DATA_RAW_RE.search(ruta_str)
    if m:
        hallazgos.append(("rastro de gaceta_rag/data/raw", m.group(0)))

    if path.suffix.lower() not in EXTENSIONES_TEXTO:
        return hallazgos

    try:
        texto = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return hallazgos

    for nombre, patron in PATRONES.items():
        for m in patron.finditer(texto):
            hallazgos.append((nombre, m.group(0)))

    return hallazgos

def verificar_directorio(directorio: Path) -> list[tuple[Path, str, str]]:
    """Devuelve lista de (archivo, nombre_patron, fragmento) para todo el árbol."""
    resultados: list[tuple[Path, str, str]] = []
    for path in directorio.rglob("*"):
        if not path.is_file():
            continue
        for nombre, fragmento in escanear_archivo(path):
            resultados.append((path, nombre, fragmento))
    return resultados

def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Uso: python scripts/verificar_publicacion.py <directorio>", file=sys.stderr)
        return 2

    directorio = Path(argv[1])
    if not directorio.is_dir():
        print(f"No es un directorio: {directorio}", file=sys.stderr)
        return 2

    hallazgos = verificar_directorio(directorio)

    if hallazgos:
        print(f"ENCONTRADOS {len(hallazgos)} problema(s) de privacidad:")
        for path, nombre, fragmento in hallazgos:
            print(f"  [{nombre}] {path}: {fragmento!r}")
        return 1

    print(f"OK: no se encontraron datos sensibles en {directorio}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
