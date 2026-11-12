# -*- coding: utf-8 -*-
"""
Respuestas EXACTAS sobre el organigrama, armadas desde la replica. Sin LLM.

Por que sin modelo
──────────────────
Contar y listar no es trabajo de un LLM: un 7B redondea ("unas veinte"), se
saltea elementos de una lista larga y no sabe de cuando es el dato. El motor
ya tiene el precedente: el PASO 0b responde "¿en que resoluciones aparece X?"
con un listado determinista, porque la exactitud no puede depender del
modelo. Esto hace lo mismo con el organigrama de MOF (PASO 0c):

  contar    -> el numero tal cual lo da la replica;
  listar    -> la lista, cortada en TOPE_LISTA con "y N mas";
  jerarquia -> de quien depende una unidad y que depende de ella, con las
               directas (hijas_de) y toda la rama (descendientes_de) por
               separado: no son lo mismo y confundirlas es el error tipico.

Cada respuesta termina diciendo de cuando son los datos ("Datos de MOF al
<fecha>"): la fecha de la ultima consulta a MOF que salio bien y, si hace mas
de 10 minutos que no la hay o fue sin auditoria, "(podría no reflejar cambios
recientes)" (vigencia.py). Sin consulta a MOF en este proceso, la fecha mas
nueva entre las unidades que se usaron para armarla.

Nunca una plantilla vacia
─────────────────────────
responder() devuelve None —y el chat sigue su camino normal— cuando esa
intencion no tiene plantilla (describir, funciones, cargos, creacion) o la
pregunta nombra una unidad que no se resolvio: no existe, o hay homonimas.

La red de abajo: respaldo()
───────────────────────────
Describir, funciones, cargos y creacion SI las redacta el modelo, con los
hechos de la unidad delante (hechos.py). Cuando esa redaccion trae una cifra
que no esta en el bloque, el motor la descarta y contesta respaldo(): los
mismos hechos, escritos para el usuario. Sale de hechos.datos(), el mismo
diccionario con el que se armo el bloque, y corta las funciones en el mismo
tope: asi la respuesta de respaldo no puede decir un numero que el modelo no
tuvo delante.
"""
from datetime import datetime

from . import hechos as _hechos
from . import replica
from . import vigencia as _vigencia

TOPE_LISTA = 40
PLANTILLAS = frozenset({"contar", "listar", "jerarquia"})

_RAMA = "en toda su rama (a cualquier nivel)"
_DIRECTAS = "que dependen directamente"

def responder(intencion, conn, vigencia=None):
    """Texto final de la respuesta, o None si no corresponde una plantilla.
    vigencia: de cuando son los datos (vigencia.de_la_consulta), o None."""
    if intencion is None or intencion.tipo not in PLANTILLAS:
        return None
    if intencion.mencion is not None and intencion.unidad_id is None:
        return None

    if intencion.unidad_id is None:
        if intencion.tipo == "jerarquia":
            return None
        cuerpo, usadas = _del_organigrama(conn, intencion)
    else:
        unidad = replica.unidad(conn, intencion.unidad_id)
        if unidad is None:
            return None
        if intencion.tipo == "jerarquia":
            cuerpo, usadas = _jerarquia(conn, unidad)
        else:
            cuerpo, usadas = _de_una_unidad(conn, intencion, unidad)
    return "\n".join(cuerpo) + "\n\n" + _fuente(usadas, vigencia)

def respaldo(intencion, hechos):
    """Los hechos de la unidad escritos para el usuario, o None si no
    corresponde. Es lo que contesta el motor cuando descarta la redaccion del
    modelo (motor_web, paso 10).

    hechos: el diccionario de hechos.datos() con el que se armo el bloque que
    leyo el modelo, para que los dos digan exactamente los mismos numeros."""
    if intencion is None or hechos is None or intencion.tipo not in _hechos.TIPOS:
        return None
    cuerpo = _RESPALDOS[intencion.tipo](hechos)
    return ("\n".join(cuerpo) + "\n\n"
            + _vigencia.linea(hechos["fecha"], hechos.get("aviso")))

def _cantidad(numero) -> str:
    return "unidad" if numero == 1 else "unidades"

def _filtro_texto(filtro, negrita=False) -> str:
    if filtro is None:
        return ""
    valor = f"**{filtro.etiqueta}**" if negrita else filtro.etiqueta
    if filtro.campo == "tipo":
        return f" de tipo {valor}"
    return f" con relación {valor}"

def _pasa(filtro, fila) -> bool:
    if filtro is None:
        return True
    if filtro.campo == "tipo":
        return (fila["tipo"] or "").casefold() == filtro.valor.casefold()
    return (fila["relacion"] or "") == filtro.valor

def _lista(filas, con_padre=False) -> list:
    lineas = []
    for k, fila in enumerate(filas[:TOPE_LISTA], 1):
        linea = f"{k}. **{fila['nombre']}**"
        if con_padre and fila["padre_nombre"]:
            linea += f" — depende de {fila['padre_nombre']}"
        lineas.append(linea)
    if len(filas) > TOPE_LISTA:
        lineas.append(f"… y {len(filas) - TOPE_LISTA} más.")
    return lineas

def _agrupadas(conn, filas, por) -> list:
    cuenta = {}
    for fila in filas:
        clave = fila["tipo"] if por == "tipo" else fila["relacion"]
        if clave:
            cuenta[clave] = cuenta.get(clave, 0) + 1
    etiquetas = ({r["relacion"]: r["etiqueta"] for r in replica.relaciones(conn)}
                 if por == "relacion" else {})
    return [f"- {etiquetas.get(clave, clave)}: **{n}**"
            for clave, n in sorted(cuenta.items(), key=lambda c: (-c[1], c[0]))]

def _por(agrupar) -> str:
    return "por tipo" if agrupar == "tipo" else "por relación"

def _fuente(filas, vigencia=None) -> str:
    """La ultima linea: la fecha de la consulta a MOF si la hubo, si no la mas
    nueva de las filas usadas; con el aviso si la vigencia lo pide."""
    fecha = vigencia.fecha if vigencia is not None else None
    if not fecha:
        fechas = [f["actualizado"] for f in filas if f["actualizado"]]
        if fechas:
            ultima = max(fechas)
            try:
                fecha = datetime.fromisoformat(ultima).strftime("%d/%m/%Y %H:%M")
            except ValueError:
                fecha = ultima
    return _vigencia.linea(fecha, vigencia is not None and vigencia.aviso)

def _del_organigrama(conn, i):
    """contar/listar sobre TODO el organigrama, con o sin filtro."""
    if i.filtro is None:
        filas = replica.todas(conn)
    elif i.filtro.campo == "tipo":
        filas = replica.por_tipo(conn, i.filtro.valor)
    else:
        filas = replica.por_relacion(conn, i.filtro.valor)
    n = len(filas)
    filtro = _filtro_texto(i.filtro, negrita=True)

    if i.agrupar:
        cuerpo = [f"El organigrama de MOF tiene **{n} {_cantidad(n)}**{filtro}, "
                  f"{_por(i.agrupar)}:", ""]
        cuerpo += _agrupadas(conn, filas, i.agrupar)
    elif i.tipo == "contar":
        cuerpo = [f"El organigrama de MOF tiene **{n} {_cantidad(n)}**{filtro}."]
    else:
        cuerpo = [f"Unidades{filtro} del organigrama de MOF: **{n}**", ""]
        cuerpo += _lista(filas, con_padre=True)
    return cuerpo, filas

def _de_una_unidad(conn, i, unidad):
    """contar/listar lo que depende de una unidad: directas y rama aparte."""
    directas = [f for f in replica.hijas_de(conn, unidad["id"]) if _pasa(i.filtro, f)]
    rama = [f for f in replica.descendientes_de(conn, unidad["id"]) if _pasa(i.filtro, f)]
    usadas = [unidad, *directas, *rama]
    filtro = _filtro_texto(i.filtro)
    en_rama = i.alcance == "rama"

    if i.tipo == "contar" and not i.agrupar:
        lineas = [f"- Unidades{filtro} {_DIRECTAS}: **{len(directas)}**",
                  f"- Unidades{filtro} {_RAMA}: **{len(rama)}**"]
        if en_rama:
            lineas.reverse()
        return [f"**{unidad['nombre']}**", ""] + lineas, usadas

    base, otra = (rama, directas) if en_rama else (directas, rama)
    alcance = _RAMA if en_rama else _DIRECTAS
    if i.agrupar:
        cuerpo = [f"**{unidad['nombre']}** — unidades{filtro} {alcance}, "
                  f"{_por(i.agrupar)}: **{len(base)}**", ""]
        cuerpo += _agrupadas(conn, base, i.agrupar)
    else:
        cuerpo = [f"**{unidad['nombre']}** — unidades{filtro} {alcance}: "
                  f"**{len(base)}**", ""]
        cuerpo += _lista(base, con_padre=en_rama)
    if en_rama:
        cuerpo += ["", f"Dependen directamente: **{len(otra)}** {_cantidad(len(otra))}."]
    else:
        cuerpo += ["", f"En toda su rama (a cualquier nivel): **{len(otra)}** "
                       f"{_cantidad(len(otra))}."]
    return cuerpo, usadas

def _jerarquia(conn, unidad):
    """De quien depende, la linea hasta la cima, y lo que depende de ella."""
    cuerpo = [f"**{unidad['nombre']}**", ""]
    usadas = [unidad]
    if not unidad["padre_id"]:
        cuerpo.append("- No depende de ninguna unidad: es la cima del organigrama.")
    else:
        cadena = _ancestros(conn, unidad)
        if not cadena:
            cuerpo.append(f"- Depende directamente de: "
                          f"**{unidad['padre_nombre'] or unidad['padre_id']}** "
                          f"(no figura en el organigrama cargado)")
        else:
            usadas += cadena
            cuerpo.append(f"- Depende directamente de: **{cadena[0]['nombre']}**")
            if len(cadena) > 1:
                cuerpo.append("- Línea hasta la cima: "
                              + " → ".join(f["nombre"] for f in cadena))

    hijas = replica.hijas_de(conn, unidad["id"])
    rama = replica.descendientes_de(conn, unidad["id"])
    usadas += [*hijas, *rama]
    cuerpo.append(f"- Unidades {_DIRECTAS} de ella: **{len(hijas)}**")
    cuerpo += ["  " + linea for linea in _lista(hijas)]
    cuerpo.append(f"- Unidades {_RAMA}: **{len(rama)}**")
    return cuerpo, usadas

def _funciones_numeradas(h) -> list:
    lineas = [f"{k}. {f}"
              for k, f in enumerate(h["funciones"][:_hechos.TOPE_FUNCIONES], 1)]
    if len(h["funciones"]) > _hechos.TOPE_FUNCIONES:
        lineas.append(f"… y {len(h['funciones']) - _hechos.TOPE_FUNCIONES} más.")
    return lineas

def _respaldo_funciones(h) -> list:
    if not h["funciones"]:
        return [f"**{h['nombre']}** — sus funciones {_hechos.NO_FIGURAN}."]
    return [f"**{h['nombre']}** — funciones en el organigrama de MOF: "
            f"**{len(h['funciones'])}**", ""] + _funciones_numeradas(h)

def _respaldo_cargos(h) -> list:
    if not h["cargos"]:
        return [f"**{h['nombre']}** — sus cargos {_hechos.NO_FIGURAN}."]
    return ([f"**{h['nombre']}** — cargos en el organigrama de MOF:", ""]
            + [f"- {cargo}" for cargo in h["cargos"]])

def _respaldo_creacion(h) -> list:
    cuerpo = [f"**{h['nombre']}**", ""]
    if h["creada_por"]:
        cuerpo.append(f"- Creada por: **{h['creada_por']}**")
    else:
        cuerpo.append(f"- La resolución que la creó {_hechos.NO_FIGURA}.")
    if h["en_la_gaceta"]:
        cuerpo.append(f"- Resolución de creación en la Gaceta: {h['en_la_gaceta']}")
    return cuerpo

def _respaldo_describir(h) -> list:
    titulo = f"**{h['nombre']}**" + (f" ({h['codigo']})" if h["codigo"] else "")
    cuerpo = [titulo, ""]
    cuerpo.append(f"- Depende de: **{h['depende_de']}**" if h["depende_de"] else
                  "- No depende de ninguna unidad: es la cima del organigrama.")
    cuerpo.append(f"- Unidades a su cargo: **{h['directas']}** directas, "
                  f"**{h['rama']}** en toda su rama")
    cuerpo.append("- Cargos: " + (", ".join(h["cargos"]) if h["cargos"]
                                  else _hechos.NO_FIGURAN))
    cuerpo.append(f"- Creada por: **{h['creada_por']}**" if h["creada_por"]
                  else f"- Creada por: {_hechos.NO_FIGURA}")
    if h["funciones"]:
        cuerpo += ["", f"Funciones en el organigrama de MOF: "
                       f"**{len(h['funciones'])}**", ""] + _funciones_numeradas(h)
    else:
        cuerpo += ["", f"Sus funciones {_hechos.NO_FIGURAN}."]
    return cuerpo

_RESPALDOS = {
    "describir": _respaldo_describir,
    "funciones": _respaldo_funciones,
    "cargos": _respaldo_cargos,
    "creacion": _respaldo_creacion,
}

def _ancestros(conn, unidad) -> list:
    cadena, vistas, actual = [], {unidad["id"]}, unidad["padre_id"]
    while actual and actual not in vistas:
        fila = replica.unidad(conn, actual)
        if fila is None:
            break
        cadena.append(fila)
        vistas.add(actual)
        actual = fila["padre_id"]
    return cadena
