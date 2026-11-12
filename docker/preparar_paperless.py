# -*- coding: utf-8 -*-
"""
Deja Paperless-ngx listo para ser la puerta de entrada (docker-compose.paperless.yml).

    python docker/preparar_paperless.py          (con Paperless ya levantado)

Se puede correr las veces que haga falta. Crea:

  gaceta            la etiqueta que marca lo que pasa a la Gaceta.
  mcp-entrada       el usuario del MCP: sube y lee, pero NO cambia ni borra. El
                    MCP trae herramientas de borrado; lo que las frena es esto.
  gaceta-lector     el usuario de la Gaceta: solo lee.
  el workflow       comparte con gaceta-lector lo etiquetado. Lo que sube
                    mcp-entrada queda como suyo y, sin esto, el lector recibe
                    404: se comprobo contra la 3.4.1.
  el webhook        avisa a la Gaceta de lo etiquetado, DESPUES de compartirlo.
                    Pide GACETA_PAPERLESS_TOKEN en el .env; sin el no se crea.
  el catalogo       etiquetas, tipos, corresponsales y rutas quedan sin dueno.
                    Con dueno nadie mas ve su nombre: un documento de tipo Acta
                    entraria a la Gaceta como Resolucion, sin aviso. Hay que
                    volver a correrlo si se crea algo con dueno (la web de
                    Paperless lo pone por defecto).

Los tokens van al .env (PAPERLESS_MCP_API_KEY, GACETA_PAPERLESS_API_KEY) y no se
muestran. Las claves de los dos usuarios son al azar y no se guardan: lo unico
que se usa es el token, y si deja de servir se saca otro.
"""
import argparse
import os
import secrets
import sys
import time
from pathlib import Path

import httpx

ETIQUETA = "gaceta"
USUARIO_MCP = "mcp-entrada"
USUARIO_LECTOR = "gaceta-lector"
WORKFLOW = "gaceta: compartir con el lector"
WORKFLOW_AVISO = "gaceta: avisar a la gaceta"
URL_GACETA = "http://gaceta:8000"

PERMISOS = {
    USUARIO_MCP: ["add_document", "view_document", "view_tag", "view_documenttype", "view_correspondent", "view_customfield",
                  "view_storagepath", "view_note", "view_paperlesstask"],
    USUARIO_LECTOR: ["view_document", "view_tag", "view_documenttype",
                     "view_correspondent", "view_customfield", "view_note"],
}
VARIABLE_TOKEN = {USUARIO_MCP: "PAPERLESS_MCP_API_KEY",
                  USUARIO_LECTOR: "GACETA_PAPERLESS_API_KEY"}

DOCUMENTO_AGREGADO = 2
DOCUMENTO_ACTUALIZADO = 3
ACCION_ASIGNAR = 1
ACCION_WEBHOOK = 4
TODOS_LOS_ORIGENES = [1, 2, 3, 4]

INTENTOS_TOKEN = 12
CATALOGO = ("tags", "document_types", "correspondents", "storage_paths")

class FaltaConfiguracion(RuntimeError):
    pass

def leer_env(ruta: Path) -> dict:
    valores = {}
    if ruta.exists():
        for linea in ruta.read_text(encoding="utf-8").splitlines():
            linea = linea.strip()
            if linea and not linea.startswith("#") and "=" in linea:
                clave, valor = linea.split("=", 1)
                valores[clave.strip()] = valor.strip().strip("'\"")
    return valores

def escribir_env(ruta: Path, nuevos: dict) -> None:
    """Llena o agrega variables sin tocar el resto del archivo, y de una vez:
    es el .env de toda la aplicacion."""
    with open(ruta, encoding="utf-8", newline="") as f:
        texto = f.read()
    fin = "\r\n" if "\r\n" in texto else "\n"
    lineas = texto.splitlines()
    for clave, valor in nuevos.items():
        for i, linea in enumerate(lineas):
            if linea.startswith(f"{clave}="):
                lineas[i] = f"{clave}={valor}"
                break
        else:
            lineas.append(f"{clave}={valor}")
    tmp = ruta.with_name(ruta.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(fin.join(lineas) + fin)
    os.replace(tmp, ruta)

class Preparador:
    def __init__(self, url, transporte=None, dormir=time.sleep):
        self.url = url.rstrip("/")
        self.transporte = transporte
        self.dormir = dormir
        self.api = None

    def _cliente(self, token) -> httpx.Client:
        return httpx.Client(
            base_url=self.url, transport=self.transporte, timeout=60,
            headers={"Authorization": f"Token {token}",
                     "Accept": "application/json; version=10"})

    def pedir_token(self, usuario, clave) -> str:
        """/api/token/ limita los intentos por minuto: se espera lo que pide."""
        with httpx.Client(base_url=self.url, transport=self.transporte,
                          timeout=60) as c:
            for _ in range(INTENTOS_TOKEN):
                r = c.post("/api/token/",
                           json={"username": usuario, "password": clave})
                if r.status_code != 429:
                    break
                self.dormir(int(r.headers.get("Retry-After") or 15) + 1)
        if r.status_code == 400:
            raise FaltaConfiguracion(
                f"Paperless rechazo la clave de {usuario}. Si cambiaste "
                "PAPERLESS_ADMIN_PASSWORD despues del primer arranque, "
                "Paperless sigue con la anterior.")
        r.raise_for_status()
        return r.json()["token"]

    def token_sirve(self, token) -> bool:
        if not token:
            return False
        with self._cliente(token) as c:
            r = c.get("/api/tags/", params={"name__iexact": ETIQUETA})
        return r.status_code == 200

    def _uno(self, ruta, **filtro):
        r = self.api.get(ruta, params=filtro)
        r.raise_for_status()
        filas = r.json()["results"]
        return filas[0] if filas else None

    def asegurar_etiqueta(self) -> int:
        fila = self._uno("/api/tags/", name__iexact=ETIQUETA)
        if fila:
            return fila["id"]
        r = self.api.post("/api/tags/", json={"name": ETIQUETA, "owner": None})
        r.raise_for_status()
        return r.json()["id"]

    def publicar_catalogo(self) -> int:
        """Saca el dueno de lo que lo tenga. -> cuantos cambio."""
        cambiados = 0
        for recurso in CATALOGO:
            pagina = 1
            while True:
                r = self.api.get(f"/api/{recurso}/", params={"page": pagina})
                r.raise_for_status()
                datos = r.json()
                for fila in datos["results"]:
                    if fila.get("owner") is not None:
                        self.api.patch(f"/api/{recurso}/{fila['id']}/",
                                       json={"owner": None}).raise_for_status()
                        cambiados += 1
                if not datos.get("next"):
                    break
                pagina += 1
        return cambiados

    def asegurar_usuario(self, nombre, token_actual) -> tuple:
        """-> (id, token nuevo o None si el actual sigue sirviendo)."""
        fila = self._uno("/api/users/", username__iexact=nombre)
        if fila and self.token_sirve(token_actual):
            r = self.api.patch(f"/api/users/{fila['id']}/",
                               json={"user_permissions": PERMISOS[nombre]})
            r.raise_for_status()
            return fila["id"], None
        clave = secrets.token_urlsafe(32)
        datos = {"password": clave, "user_permissions": PERMISOS[nombre]}
        if fila:
            r = self.api.patch(f"/api/users/{fila['id']}/", json=datos)
        else:
            r = self.api.post("/api/users/", json=dict(
                datos, username=nombre, is_active=True))
        r.raise_for_status()
        return r.json()["id"], self.pedir_token(nombre, clave)

    def asegurar_workflow(self, etiqueta, lector) -> None:
        deseado = {
            "name": WORKFLOW, "order": 0, "enabled": True,
            "triggers": [{"type": t, "sources": TODOS_LOS_ORIGENES,
                          "filter_has_tags": [etiqueta]}
                         for t in (DOCUMENTO_AGREGADO, DOCUMENTO_ACTUALIZADO)],
            "actions": [{"type": ACCION_ASIGNAR, "assign_view_users": [lector]}],
        }
        fila = next((w for w in self.api.get("/api/workflows/").json()["results"]
                     if w["name"] == WORKFLOW), None)
        if fila:
            if self._workflow_al_dia(fila, etiqueta, lector):
                return
            self.api.delete(f"/api/workflows/{fila['id']}/").raise_for_status()
        self.api.post("/api/workflows/", json=deseado).raise_for_status()

    def asegurar_webhook(self, etiqueta, token, url_gaceta) -> None:
        gancho = {"url": f"{url_gaceta.rstrip('/')}/api/paperless/evento",
                  "use_params": False, "as_json": False,
                  "include_document": False,
                  "body": '{"doc_id": {{ doc_id }}}',
                  "headers": {"X-Admin-Token": token}}
        deseado = {
            "name": WORKFLOW_AVISO, "order": 1, "enabled": True,
            "triggers": [{"type": t, "sources": TODOS_LOS_ORIGENES,
                          "filter_has_tags": [etiqueta]}
                         for t in (DOCUMENTO_AGREGADO, DOCUMENTO_ACTUALIZADO)],
            "actions": [{"type": ACCION_WEBHOOK, "webhook": gancho}],
        }
        fila = next((w for w in self.api.get("/api/workflows/").json()["results"]
                     if w["name"] == WORKFLOW_AVISO), None)
        if fila:
            if self._webhook_al_dia(fila, etiqueta, gancho):
                return
            self.api.delete(f"/api/workflows/{fila['id']}/").raise_for_status()
        self.api.post("/api/workflows/", json=deseado).raise_for_status()

    @staticmethod
    def _webhook_al_dia(fila, etiqueta, gancho) -> bool:
        actual = (fila["actions"][0].get("webhook") or {}) if fila["actions"] else {}
        return (sorted(t["type"] for t in fila["triggers"])
                == [DOCUMENTO_AGREGADO, DOCUMENTO_ACTUALIZADO]
                and all(t["filter_has_tags"] == [etiqueta]
                        and sorted(t["sources"]) == TODOS_LOS_ORIGENES
                        for t in fila["triggers"])
                and all(actual.get(k) == v for k, v in gancho.items()))

    @staticmethod
    def _workflow_al_dia(fila, etiqueta, lector) -> bool:
        tipos = sorted(t["type"] for t in fila["triggers"])
        return (tipos == [DOCUMENTO_AGREGADO, DOCUMENTO_ACTUALIZADO]
                and all(t["filter_has_tags"] == [etiqueta]
                        and sorted(t["sources"]) == TODOS_LOS_ORIGENES
                        for t in fila["triggers"])
                and len(fila["actions"]) == 1
                and fila["actions"][0]["assign_view_users"] == [lector])

def preparar(url, ruta_env, transporte=None, dormir=time.sleep) -> dict:
    """-> {"tokens_nuevos": [nombres de variable]}. Nunca devuelve un token."""
    ruta_env = Path(ruta_env)
    env = leer_env(ruta_env)
    clave = env.get("PAPERLESS_ADMIN_PASSWORD") or os.environ.get("PAPERLESS_ADMIN_PASSWORD")
    if not clave:
        raise FaltaConfiguracion(f"Falta PAPERLESS_ADMIN_PASSWORD en {ruta_env}.")
    usuario = env.get("PAPERLESS_ADMIN_USER") or "admin"

    p = Preparador(url, transporte=transporte, dormir=dormir)
    token_admin = p.pedir_token(usuario, clave)
    nuevos = {}
    with p._cliente(token_admin) as p.api:
        etiqueta = p.asegurar_etiqueta()
        ids = {}
        for nombre, variable in VARIABLE_TOKEN.items():
            ids[nombre], token = p.asegurar_usuario(nombre, env.get(variable))
            if token:
                nuevos[variable] = token
        p.asegurar_workflow(etiqueta, ids[USUARIO_LECTOR])
        token_webhook = env.get("GACETA_PAPERLESS_TOKEN")
        if token_webhook:
            p.asegurar_webhook(etiqueta, token_webhook,
                               env.get("GACETA_URL_INTERNA") or URL_GACETA)
        publicados = p.publicar_catalogo()
    if nuevos:
        escribir_env(ruta_env, nuevos)
    return {"tokens_nuevos": sorted(nuevos), "catalogo_publicado": publicados,
            "webhook": bool(token_webhook)}

def main() -> int:
    raiz = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env", default=str(raiz / ".env"))
    ap.add_argument("--url", help="por defecto http://127.0.0.1:PAPERLESS_PUERTO")
    args = ap.parse_args()
    puerto = leer_env(Path(args.env)).get("PAPERLESS_PUERTO") or "8002"
    url = args.url or f"http://127.0.0.1:{puerto}"
    try:
        r = preparar(url, args.env)
    except FaltaConfiguracion as exc:
        print(exc, file=sys.stderr)
        return 1
    except httpx.TransportError as exc:
        print(f"No se pudo hablar con Paperless en {url}: {exc}. Si recien lo "
              "levantaste, espera un minuto y volve a correrlo.", file=sys.stderr)
        return 1
    print(f"Paperless listo: etiqueta '{ETIQUETA}', usuarios {USUARIO_MCP} y "
          f"{USUARIO_LECTOR}, workflow de lectura.")
    if not r["webhook"]:
        print("Sin GACETA_PAPERLESS_TOKEN en el .env: no se creo el webhook, y la "
              "Gaceta no se entera de lo que llega.")
    if r["catalogo_publicado"]:
        print(f"Catalogo: {r['catalogo_publicado']} sin dueno desde ahora.")
    if r["tokens_nuevos"]:
        print(f"Tokens nuevos en {args.env}: {', '.join(r['tokens_nuevos'])}.")
        print("Volve a levantar los servicios para que los tomen.")
    else:
        print("Los tokens que ya estaban siguen sirviendo.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
