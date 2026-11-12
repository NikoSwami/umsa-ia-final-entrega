# -*- coding: utf-8 -*-
"""
Cliente HTTP minimo contra la API REST de Paperless-ngx.

Solo LEE: no hay un solo POST, PUT ni DELETE en este archivo, y el token es el
de gaceta-lector, que tampoco podria (docker/preparar_paperless.py).

Quien lo usa decide con estas respuestas si un documento se da de baja, asi que
cada caso se distingue:

  None                el documento no existe PARA ESTE TOKEN. Paperless tambien
                      responde 404 si falta el permiso y si esta en la papelera:
                      por si solo no prueba un borrado.
  NoAutorizado        401 o 403. No se arregla esperando.
  SinConexion         red caida o timeout. Se arregla esperando.
  ListadoIncompleto   el listado no cuadra. Lo que falto NO se borro.

Sin GACETA_PAPERLESS_URL o sin GACETA_PAPERLESS_API_KEY queda `configurado =
False` y quien lo use debe tratarlo como un no-op, igual que el cliente de MOF.
"""
import os
import re
import threading
from urllib.parse import unquote

import httpx

TIMEOUT = float(os.environ.get("PAPERLESS_TIMEOUT") or "30")
TIMEOUT_DESCARGA = 120.0
MAX_BYTES = int(float(os.environ.get("CARGA_MAX_MB") or 100) * 1024 * 1024)
VERSION_API = 10
POR_PAGINA = 100

_RE_NOMBRE_UTF8 = re.compile(r"filename\*=utf-8''([^;]+)", re.IGNORECASE)
_RE_NOMBRE = re.compile(r'filename="?([^";]+)"?', re.IGNORECASE)

class ErrorPaperless(RuntimeError):
    """Fallo hablando con Paperless. `codigo` es el HTTP, o None si ni siquiera
    hubo respuesta."""

    def __init__(self, mensaje: str, codigo: int = None):
        super().__init__(mensaje)
        self.codigo = codigo

class SinConexion(ErrorPaperless):
    pass

class NoAutorizado(ErrorPaperless):
    pass

class VersionNoSoportada(ErrorPaperless):
    pass

class ListadoIncompleto(ErrorPaperless):
    pass

class ArchivoDemasiadoGrande(ErrorPaperless):
    pass

class ClientePaperless:
    def __init__(self, url: str = None, token: str = None, timeout: float = None,
                 max_bytes: int = None, transport=None):
        self.url = (url if url is not None
                    else os.environ.get("GACETA_PAPERLESS_URL", "")).rstrip("/")
        self.token = (token if token is not None
                      else os.environ.get("GACETA_PAPERLESS_API_KEY", ""))
        self.timeout = timeout or TIMEOUT
        self.max_bytes = max_bytes or MAX_BYTES
        self._transport = transport
        self._nombres = {}
        self._lock = threading.Lock()

    @property
    def configurado(self) -> bool:
        return bool(self.url and self.token)

    def _cliente(self, timeout=None) -> httpx.Client:
        if not self.configurado:
            raise ErrorPaperless("Paperless no esta configurado")
        extra = {"transport": self._transport} if self._transport else {}
        return httpx.Client(
            base_url=self.url, timeout=timeout or self.timeout,
            headers={"Authorization": f"Token {self.token}",
                     "Accept": f"application/json; version={VERSION_API}"},
            **extra)

    @staticmethod
    def _revisar(r: httpx.Response, ruta: str) -> None:
        if r.status_code in (401, 403):
            raise NoAutorizado(f"GET {ruta} -> {r.status_code}", codigo=r.status_code)
        if r.status_code == 406:
            raise VersionNoSoportada(
                f"GET {ruta} -> 406: Paperless no acepta la version {VERSION_API} "
                "de la API", codigo=406)
        if r.status_code >= 400:
            raise ErrorPaperless(f"GET {ruta} -> {r.status_code}", codigo=r.status_code)

    def _get(self, ruta: str, params: dict = None, vacio_si_404=False):
        try:
            with self._cliente() as http:
                r = http.get(ruta, params=params)
        except httpx.HTTPError as exc:
            raise SinConexion(f"GET {ruta}: {exc.__class__.__name__}") from None
        if r.status_code == 404 and vacio_si_404:
            return None
        self._revisar(r, ruta)
        try:
            return r.json()
        except ValueError:
            raise ErrorPaperless(f"GET {ruta}: la respuesta no es JSON",
                                 codigo=r.status_code) from None

    def _todas_las_filas(self, ruta: str, params: dict = None) -> list:
        """Todas las paginas, o una excepcion: nunca una lista a medias. Un
        documento que falta aqui se daria por borrado."""
        filas = []
        pagina = 1
        while True:
            datos = self._get(ruta, params=dict(
                params or {}, page=pagina, page_size=POR_PAGINA))
            esta = datos.get("results") or []
            filas += esta
            total = datos.get("count")
            if not datos.get("next"):
                break
            if not esta or (total is not None and len(filas) >= total):
                raise ListadoIncompleto(f"GET {ruta}: la paginacion no termina")
            pagina += 1
        if total is not None and len(filas) != total:
            raise ListadoIncompleto(
                f"GET {ruta}: anuncia {total} filas y llegaron {len(filas)}")
        return filas

    def documento(self, doc_id):
        """El documento, o None si no existe para este token (ver arriba)."""
        return self._get(f"/api/documents/{int(doc_id)}/", vacio_si_404=True)

    def original(self, doc_id):
        """-> (nombre, bytes) del archivo tal como se subio, o None si no existe
        para este token. Se corta apenas pasa del limite, sin bajarlo entero."""
        ruta = f"/api/documents/{int(doc_id)}/download/"
        datos = bytearray()
        try:
            with self._cliente(timeout=TIMEOUT_DESCARGA) as http:
                with http.stream("GET", ruta, params={"original": "true"}) as r:
                    if r.status_code == 404:
                        return None
                    self._revisar(r, ruta)
                    anunciado = r.headers.get("Content-Length")
                    if anunciado and int(anunciado) > self.max_bytes:
                        raise ArchivoDemasiadoGrande(
                            f"el documento {doc_id} pesa {anunciado} bytes")
                    for trozo in r.iter_bytes():
                        datos += trozo
                        if len(datos) > self.max_bytes:
                            raise ArchivoDemasiadoGrande(
                                f"el documento {doc_id} pasa de {self.max_bytes} bytes")
                    nombre = _nombre_de(r.headers.get("Content-Disposition"))
        except httpx.HTTPError as exc:
            raise SinConexion(f"GET {ruta}: {exc.__class__.__name__}") from None
        return nombre or f"documento-{int(doc_id)}", bytes(datos)

    def listar_con_etiqueta(self, nombre: str) -> list:
        """[{id, modified}] de TODO lo que lleva la etiqueta. Orden por id para
        que la paginacion no se mueva, y sin el texto: pesa megas por documento."""
        filas = self._todas_las_filas("/api/documents/", {
            "tags__name__iexact": nombre, "ordering": "id",
            "fields": "id,modified"})
        return [{"id": f["id"], "modified": f.get("modified")} for f in filas]

    def _nombre(self, recurso: str, id_):
        """El nombre de un id, o None. Lista guardada: se renueva sola si
        aparece un id que no conoce (alguien creo una etiqueta)."""
        if id_ is None:
            return None
        with self._lock:
            conocido = self._nombres.get(recurso, {}).get(id_)
        if conocido is not None:
            return conocido
        filas = self._todas_las_filas(f"/api/{recurso}/")
        tabla = {f["id"]: f.get("name") for f in filas}
        with self._lock:
            self._nombres[recurso] = tabla
        return tabla.get(id_)

    def catalogo(self, recurso: str) -> dict:
        """{id: nombre} de todo un recurso ('tags', 'document_types'...), leido
        ahora. Queda guardado para los nombres."""
        tabla = {f["id"]: f.get("name") for f in self._todas_las_filas(f"/api/{recurso}/")}
        with self._lock:
            self._nombres[recurso] = tabla
        return dict(tabla)

    def nombre_etiqueta(self, id_):
        return self._nombre("tags", id_)

    def nombre_tipo(self, id_):
        return self._nombre("document_types", id_)

    def nombre_corresponsal(self, id_):
        return self._nombre("correspondents", id_)

    def nombre_campo(self, id_):
        return self._nombre("custom_fields", id_)

def _nombre_de(cabecera):
    if not cabecera:
        return None
    m = _RE_NOMBRE_UTF8.search(cabecera)
    if m:
        return unquote(m.group(1).strip())
    m = _RE_NOMBRE.search(cabecera)
    return m.group(1).strip() if m else None
