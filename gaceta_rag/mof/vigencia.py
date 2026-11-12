# -*- coding: utf-8 -*-
"""
De cuando son los datos del organigrama que contesta el chat.

La consulta automatica a MOF (novedades.Consultor) puede fallar un rato: MOF
reiniciandose, credenciales vencidas. El chat no se bloquea —contesta con la
replica que tiene—, pero dice de cuando es lo que contesta:

  - "Datos de MOF al <fecha>": la de la ultima consulta que salio bien, en
    hora local (la misma que `actualizado` en la replica);
  - "(podría no reflejar cambios recientes)" si hace mas de 10 minutos que no
    hay una consulta ok —o nunca la hubo en este proceso—, o si fue sin
    auditoria: sin ella, un cambio en una funcion llega recien con la
    reconciliacion profunda.

Sin consultor (pruebas, eval, consola: nadie le pregunta a MOF) no hay nada
que decir y la fecha es la de la replica, como antes.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

LIMITE = timedelta(minutes=10)
AVISO = "podría no reflejar cambios recientes"

@dataclass(frozen=True)
class Vigencia:
    fecha: str = None
    aviso: bool = False

def de_la_consulta(estado, ahora=None):
    """La Vigencia que corresponde a Consultor.estado(), o None sin consultor.
    Una fecha que no se puede leer cuenta como consulta que no salio bien:
    ante la duda, se avisa."""
    if estado is None:
        return None
    ahora = ahora or datetime.now(timezone.utc)
    ultimo_ok = estado.get("ultimo_ok")
    if ultimo_ok is None and estado.get("ok"):
        ultimo_ok = estado.get("cuando")
    cuando = _parse(ultimo_ok)
    sin_auditoria = "sin_auditoria" in (estado.get("modo"), estado.get("modo_ultimo_ok"))
    if cuando is None:
        return Vigencia(None, True)
    return Vigencia(cuando.astimezone().strftime("%d/%m/%Y %H:%M"),
                    sin_auditoria or ahora - cuando > LIMITE)

def linea(fecha, aviso) -> str:
    """La ultima linea de cada respuesta del organigrama."""
    base = f"Datos de MOF al {fecha}" if fecha else "Datos de MOF sin fecha"
    return f"{base} ({AVISO})." if aviso else f"{base}."

def _parse(iso):
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
