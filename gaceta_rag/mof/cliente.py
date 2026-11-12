# -*- coding: utf-8 -*-
"""
Cliente HTTP minimo contra la API REST de MOF (NestJS).

Solo LEE. No hay un solo POST, PUT ni DELETE hacia MOF en este archivo, salvo
el login. Se usa para tres cosas:

  - Consultar novedades (novedades.py): la Gaceta le pregunta a la auditoria de
    MOF que unidades cambiaron desde la ultima vez, y vuelve a leer SOLO esas.
  - Leer el ESTADO ACTUAL de una unidad. Nunca se usan los datos que trae la
    auditoria: si dos cambios se cruzan, gana lo que MOF dice ahora y no el
    orden en que llegaron las filas.
  - Reconciliar: recorrer todas las unidades y comparar con lo local
    (reconciliar.py), la red que recoge lo que la consulta no vea.

Autenticacion: POST /auth/login con {email, password} devuelve un JWT que se
reusa mientras no de 401. Las credenciales salen del entorno, nunca del codigo.
MOF pide sesion para TODO: la auditoria (GET /versiones) y, desde
AIVERKE/MOF fa691c6 (16/9/2026), tambien las lecturas de unidades. Sin
MOF_USUARIO/MOF_PASSWORD —una cuenta propia de rol USER— la Gaceta no puede
leer el organigrama (ver aviso_sin_credenciales).

Sin MOF configurado (MOF_URL vacio) el cliente queda `configurado = False` y
quien lo use debe tratarlo como un no-op, igual que el resto de los canales
opcionales del motor.
"""
import os
import threading

TIMEOUT = float(os.environ.get("MOF_TIMEOUT") or "8")

class ErrorMOF(RuntimeError):
    """Fallo hablando con MOF.

    `codigo` es el HTTP de la respuesta, o None si ni siquiera hubo respuesta
    (red caida, timeout). Importa distinguirlos: un 401 no se arregla solo
    -falta el permiso, hay que usar otro camino-; un None si, esperando.
    """

    def __init__(self, mensaje: str, codigo: int = None):
        super().__init__(mensaje)
        self.codigo = codigo

class ClienteMOF:
    def __init__(self, url: str = None, usuario: str = None,
                 password: str = None, timeout: float = None,
                 transport=None):
        self.url = (url if url is not None
                    else os.environ.get("MOF_URL", "")).rstrip("/")
        self.usuario = usuario if usuario is not None else os.environ.get("MOF_USUARIO", "")
        self.password = password if password is not None else os.environ.get("MOF_PASSWORD", "")
        self.timeout = timeout or TIMEOUT
        self._transport = transport
        self._token = None
        self._lock = threading.Lock()

    @property
    def configurado(self) -> bool:
        return bool(self.url)

    @property
    def aviso_sin_credenciales(self):
        """Por que la consulta automatica no va a poder leer nada, o None."""
        if not self.configurado or (self.usuario and self.password):
            return None
        return ("MOF_URL esta configurado pero faltan MOF_USUARIO/MOF_PASSWORD: "
                "MOF pide sesion tambien para leer el organigrama, asi que la "
                "Gaceta no va a poder mantenerlo al dia. Hace falta una cuenta "
                "propia de rol USER.")

    def _cliente(self):
        import httpx
        extra = {"transport": self._transport} if self._transport else {}
        return httpx.Client(base_url=self.url, timeout=self.timeout, **extra)

    def _login(self, http) -> str:
        if not (self.usuario and self.password):
            return ""
        r = http.post("/auth/login",
                      json={"email": self.usuario, "password": self.password})
        if r.status_code >= 400:
            raise ErrorMOF(f"login rechazado ({r.status_code})",
                           codigo=401 if r.status_code in (400, 401, 403)
                           else r.status_code)
        datos = r.json() or {}
        token = (datos.get("access_token") or datos.get("accessToken")
                 or datos.get("token") or "")
        if not token:
            raise ErrorMOF("el login no devolvio token")
        return token

    def _get(self, ruta: str, params: dict = None, vacio_si_404=False):
        """GET con reintento unico tras renovar el token si dio 401.

        vacio_si_404: para recursos que pueden haber dejado de existir (una
        unidad borrada). Ahi un 404 es una respuesta, no un fallo.
        """
        import httpx

        if not self.configurado:
            raise ErrorMOF("MOF_URL no configurado")
        try:
            with self._cliente() as http:
                for intento in (1, 2):
                    with self._lock:
                        if self._token is None:
                            self._token = self._login(http)
                        token = self._token
                    cab = {"Authorization": f"Bearer {token}"} if token else {}
                    r = http.get(ruta, headers=cab, params=params)
                    if r.status_code == 401 and intento == 1 and token:
                        with self._lock:
                            self._token = None
                        continue
                    if r.status_code == 404 and vacio_si_404:
                        return None
                    if r.status_code >= 400:
                        raise ErrorMOF(f"GET {ruta} -> {r.status_code}",
                                       codigo=r.status_code)
                    return _desenvolver(r.json())
        except httpx.HTTPError as exc:
            raise ErrorMOF(f"GET {ruta}: {exc.__class__.__name__}") from exc
        raise ErrorMOF(f"GET {ruta} sin respuesta util")

    def unidades(self) -> list:
        datos = self._get("/api/v1/mof/unidades")
        return datos if isinstance(datos, list) else []

    def unidad(self, unidad_id) -> dict:
        """Detalle de una unidad, o {} si ya no existe.

        MOF responde 404 para una unidad inexistente y tambien para una dada de
        baja (softRemove: findOne no la ve). Antes eso subia como error y la
        rama que convierte "desaparecio" en una baja no se ejecutaba nunca: la
        unidad borrada seguia respondiendo en el chat."""
        return self._get(f"/api/v1/mof/unidades/{int(unidad_id)}",
                         vacio_si_404=True) or {}

    def funciones(self, unidad_id) -> list:
        datos = self._get(f"/api/v1/mof/unidades/{int(unidad_id)}/funciones")
        return datos if isinstance(datos, list) else []

    def personal(self, unidad_id) -> list:
        datos = self._get(f"/api/v1/unidades/{int(unidad_id)}/personal")
        return datos if isinstance(datos, list) else []

    def cambios(self, desde: str, pagina: int = 1, limite: int = 200) -> list:
        """Filas de la auditoria de MOF creadas desde `desde` (ISO 8601).

        Es GET /versiones: la tabla auditoria_cambio, que MOF escribe dentro de
        la MISMA transaccion que cada cambio de unidad o de funcion. MOF las
        devuelve de la mas nueva a la mas vieja, paginadas (tope 200).

        Exige token: sin MOF_USUARIO/MOF_PASSWORD responde 401.

        De cada fila solo sirven `id`, `unidadAfectadaId`, `accion` y
        `createdAt`. Tambien trae `datosAnteriores`/`datosNuevos`, y NO se usan
        a proposito: el estado se vuelve a leer de MOF (ver arriba).
        """
        datos = self._get("/versiones", params={
            "fecha_desde": desde, "page": int(pagina), "limit": int(limite)})
        return datos if isinstance(datos, list) else []

    def relaciones(self) -> list:
        """Catalogo de relaciones: [{codigo, descripcion}].

        Hace falta porque la etiqueta legible ('Lineal') solo viaja en el
        LISTADO de unidades; el detalle manda el codigo a secas. Como tanto el
        empuje como la sincronizacion trabajan con el detalle, sin esta
        peticion la etiqueta no llegaria nunca. Son tres filas y cambia casi
        nunca: una peticion por corrida.
        """
        datos = self._get("/api/v1/mof/relaciones")
        return datos if isinstance(datos, list) else []

    def unidad_completa(self, unidad_id) -> dict:
        """Todo lo que necesita una ficha, con las menos peticiones posibles.

        El detalle (mapDetail en unidades.service.ts) ya trae DENTRO las
        funciones y el padre como objeto {id, codigo, nombre}: pedirlos aparte
        eran dos viajes de mas por unidad, y con ~159 unidades eso es lo que
        separa una reconciliacion profunda de un minuto de una de tres.

        El personal es lo unico que si necesita su propia peticion, y es
        OPCIONAL a proposito: si ese endpoint falla (permisos, por ejemplo) la
        ficha se genera igual sin la seccion de cargos, en vez de perder la
        unidad entera.
        """
        unidad = self.unidad(unidad_id)
        if not unidad:
            return {}

        funciones = unidad.get("funciones")
        if not isinstance(funciones, list):
            try:
                funciones = self.funciones(unidad_id)
            except ErrorMOF:
                funciones = []

        try:
            personal = self.personal(unidad_id)
        except ErrorMOF:
            personal = []

        padre = unidad.get("parent")
        if not isinstance(padre, dict):
            padre = None
            pid = unidad.get("parentId") or unidad.get("parent_id")
            if pid:
                try:
                    padre = self.unidad(pid)
                except ErrorMOF:
                    padre = None

        return {"unidad": unidad, "funciones": funciones,
                "personal": personal, "padre": padre}

def _desenvolver(payload):
    """MOF envuelve todo en ResultResponse: {message, data}. Devuelve `data`
    si viene envuelto, o el payload tal cual si no."""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload
