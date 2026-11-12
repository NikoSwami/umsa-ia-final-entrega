# -*- coding: utf-8 -*-
"""
El orquestador de la carga manual.

Cada archivo (o texto) que se sube es un TRABAJO que pasa por estos estados:

    en_cola -> extrayendo -> revision -> indexando -> indexado
                    \\            \\           \\
                     +-> error    +-> (descartado)

  extrayendo  capa de texto del PDF u OCR (minutos en CPU para un escaneo).
  revision    el texto y el titulo propuestos esperan que alguien los mire y
              confirme. Con revisar=False se salta, salvo que la revision
              encuentre un error que bloquea (sin texto, duplicado...).
  indexando   chunks + vectores en la base viva, verificacion, y el indice a
              disco.
  indexado    buscable en el chat. Despues, en segundo plano, el grafo (LLM):
              estado aparte en trabajo['grafo'].

El trabajo pesado va por tres CARRILES, cada uno con un solo hilo y en orden:

  lectura  capa de texto u OCR. Uno a la vez: el OCR ocupa 2-4 GB y dos PDFs
           en paralelo no terminan antes, se quedan sin memoria.
  indice   indexar (el indice vivo tiene un solo escritor).
  grafo    el LLM y los pasos derivados (~1 min sobre el corpus entero).

Separados porque cada uno tarda lo suyo: con un solo hilo, corregir el titulo
de un documento (segundos) esperaba a que terminara el OCR de un escaneo de
diez paginas (en CPU, una hora). Lo descubrio la prueba de punta a punta.

Todo trabajo vive en disco (data/cargas/<id>/trabajo.json, junto al archivo
original y al Markdown del OCR). Si el proceso muere a mitad, al volver a
arrancar los trabajos siguen donde estaban: lo que se estaba leyendo se
vuelve a leer, lo que se estaba indexando se vuelve a indexar (es idempotente)
y la reparacion compara cada documento cargado con lo que hay en la base.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import queue
import secrets
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from gaceta_rag import generar_base as gb

from . import extraccion, grafo, ids, revision

ESTADOS = ('en_cola', 'extrayendo', 'revision', 'indexando', 'indexado', 'error')
CARRILES = ('lectura', 'indice', 'grafo')
CARRIL_DE = {'extraer': 'lectura', 'indexar': 'indice',
             'grafo': 'grafo', 'derivados': 'grafo'}
TIPO_POR_DEFECTO = 'Resolución'
MAX_MB = float(os.environ.get('CARGA_MAX_MB') or 100)
CAMPOS_EDITABLES = ('titulo', 'tipo_documento', 'etiquetas', 'contenido')

class ErrorCarga(ValueError):
    """Pedido invalido (con un mensaje para mostrar)."""

class NoEncontrado(LookupError):
    pass

class Conflicto(RuntimeError):
    """El pedido no corresponde al estado actual del trabajo o documento."""

class RevisionConErrores(ErrorCarga):
    def __init__(self, revision_resultado):
        self.revision = revision_resultado
        mensajes = '; '.join(e['mensaje'] for e in revision_resultado['errores'])
        super().__init__(f'La revision encontro errores: {mensajes}')

def _ahora() -> str:
    return datetime.now().isoformat(timespec='seconds')

def _escribir_json(ruta: Path, datos) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    tmp = ruta.with_suffix(ruta.suffix + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(datos, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ruta)

def _etiquetas(valor) -> list:
    if valor is None:
        return []
    if isinstance(valor, str):
        valor = valor.split(',')
    return [str(e).strip() for e in valor if str(e).strip()]

class Cargador:
    """Trabajos de carga sobre la base viva del motor.

    motor      el MotorGaceta (o un doble con faiss_index, faiss_meta,
               modelo_emb, conn, cache_titulos, llm, _lock).
    ingestor   el MISMO Ingestor que usa MOF: es el unico escritor del indice.
    motor_ocr  ocr.MotorOCRRemoto / MotorOCRPaddle, o None (sin OCR).
    """

    def __init__(self, motor, ingestor, motor_ocr=None, carpeta=None,
                 ruta_grafo_llm=None, ejecutar=None, persistir_inmediato=True,
                 recargar_indices=None):
        self.motor = motor
        self.ingestor = ingestor
        self.motor_ocr = motor_ocr
        self.raw = Path(ingestor.raw)
        self.carpeta = Path(carpeta) if carpeta else self.raw.parent / 'cargas'
        self.ruta_grafo_llm = (Path(ruta_grafo_llm) if ruta_grafo_llm
                               else self.raw.parent / 'grafo_llm.json')
        self.persistir_inmediato = persistir_inmediato
        self._ejecutar = ejecutar
        self._recargar = recargar_indices or grafo.recargar_indices
        self._lock = threading.RLock()
        self._lock_indice = threading.Lock()
        self._colas = {carril: queue.Queue() for carril in CARRILES}
        self._trabajos = {}
        self._pendientes_grafo = set()
        self._grafo_encolado = False
        self._hilo = None
        self.listo = False
        self.ultimo_grafo = None
        self.reparacion = None
        self._estado_disco = self._leer_estado()
        self._cargar_trabajos()

    @property
    def _ruta_estado(self) -> Path:
        return self.carpeta / 'estado.json'

    def _leer_estado(self) -> dict:
        try:
            with open(self._ruta_estado, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def _guardar_estado(self):
        _escribir_json(self._ruta_estado, self._estado_disco)

    def _dir(self, tid) -> Path:
        return self.carpeta / tid

    def _guardar(self, t: dict):
        t['actualizado'] = _ahora()
        _escribir_json(self._dir(t['id']) / 'trabajo.json', t)

    def _cargar_trabajos(self):
        if not self.carpeta.is_dir():
            return
        for ruta in sorted(self.carpeta.glob('*/trabajo.json')):
            try:
                with open(ruta, encoding='utf-8') as f:
                    t = json.load(f)
                self._trabajos[t['id']] = t
            except Exception as exc:
                print(f'[carga] trabajo ilegible {ruta}: {exc}')

    def iniciar(self):
        """Arranca los carriles. Primero espera a que el motor cargue el
        indice, recupera lo que quedo a medias y repara desfases; recien ahi
        los tres hilos atienden sus colas."""
        if self._hilo is not None:
            return
        self._hilo = threading.Thread(target=self._arrancar, daemon=True,
                                      name='carga-manual')
        self._hilo.start()

    def _arrancar(self):
        while not self.ingestor.disponible:
            time.sleep(2)
        try:
            self._recuperar()
        except Exception as exc:
            print(f'[carga] no se pudo recuperar el estado anterior: {exc}')
        self.listo = True
        for carril in CARRILES:
            threading.Thread(target=self._bucle, args=(carril,), daemon=True,
                             name=f'carga-{carril}').start()

    def _bucle(self, carril):
        cola = self._colas[carril]
        while True:
            accion, dato = cola.get()
            try:
                self.procesar(accion, dato)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f'[carga] {accion} {dato}: {exc}')

    def procesar(self, accion, dato):
        """Ejecuta UNA tarea de la cola. Publico para los tests (sin hilo)."""
        if accion == 'extraer':
            self._extraer(dato)
        elif accion == 'indexar':
            self._indexar(dato)
        elif accion == 'grafo':
            self._enriquecer()
        elif accion == 'derivados':
            pasos = self._derivados()
            self.ultimo_grafo = {'cuando': _ahora(), 'motivo': dato,
                                 'ok': all(p['ok'] for p in pasos),
                                 'pasos': pasos}
        else:
            raise ValueError(f'accion desconocida {accion!r}')

    def procesar_cola(self):
        """Vacia las colas en este hilo (tests y scripts sin servidor). Una
        tarea puede encolar otra en otro carril: se repite hasta que no queda
        nada en ninguno."""
        while True:
            for carril in CARRILES:
                try:
                    accion, dato = self._colas[carril].get_nowait()
                    break
                except queue.Empty:
                    continue
            else:
                return
            self.procesar(accion, dato)

    def _encolar(self, accion, dato=None):
        self._colas[CARRIL_DE[accion]].put((accion, dato))

    def _nuevo(self, origen, opciones) -> dict:
        tid = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        return {
            'id': tid, 'origen': origen, 'creado': _ahora(), 'actualizado': None,
            'estado': 'en_cola', 'opciones': opciones, 'archivo': None,
            'progreso': None, 'borrador': None, 'extraccion': None,
            'revision': None, 'doc_id': None, 'resultado': None,
            'grafo': None, 'error': None,
        }

    @staticmethod
    def _opciones(modo='auto', revisar=True, enriquecer=True, titulo=None,
                  tipo_documento=None, etiquetas=None, paginas_max=0,
                  permitir_duplicado=False) -> dict:
        modo = (modo or 'auto').strip().lower()
        if modo not in extraccion.MODOS:
            raise ErrorCarga(f'Modo desconocido: {modo!r} (auto, ocr o texto).')
        return {'modo': modo, 'revisar': bool(revisar),
                'enriquecer': bool(enriquecer),
                'titulo': (titulo or '').strip() or None,
                'tipo_documento': (tipo_documento or '').strip() or None,
                'etiquetas': _etiquetas(etiquetas) if etiquetas else None,
                'paginas_max': max(0, int(paginas_max or 0)),
                'permitir_duplicado': bool(permitir_duplicado)}

    def crear_desde_archivo(self, nombre, datos: bytes, raw_extra=None,
                            avisos_origen=None, doc_id=None, **opciones) -> list:
        """Un trabajo por archivo (un JSON con varios documentos da varios).

        Para quien trae el archivo de otro sistema (gaceta_rag/paperless):
        raw_extra      campos que se guardan tal cual en el raw.
        avisos_origen  avisos que acompanan a cada revision de este trabajo.
        doc_id         reemplaza a ese documento al confirmarse, en vez de crear
                       otro. Solo del rango manual: nunca uno del corpus.
        """
        if doc_id is not None and not ids.es_carga_manual(doc_id):
            raise ErrorCarga(f'{doc_id} no es de la carga manual: no se reemplaza.')
        nombre = extraccion.nombre_seguro(nombre)
        tipo = extraccion.tipo_de_archivo(nombre)
        if tipo is None:
            raise ErrorCarga(f'Formato no soportado: {nombre}. Se aceptan '
                             f'{", ".join(extraccion.EXTENSIONES)}.')
        if not datos:
            raise ErrorCarga(f'{nombre} esta vacio.')
        if len(datos) > MAX_MB * 1024 * 1024:
            raise ErrorCarga(f'{nombre} pesa {len(datos) / 2**20:.0f} MB; el '
                             f'maximo es {MAX_MB:.0f} MB (CARGA_MAX_MB).')
        opts = self._opciones(**opciones)
        archivo = {'nombre': nombre, 'tipo': tipo, 'bytes': len(datos),
                   'sha256': hashlib.sha256(datos).hexdigest()}

        if tipo == 'json':
            if raw_extra is not None:
                raise ErrorCarga(f'{nombre}: un JSON no se acepta desde otro '
                                 'sistema.')
            try:
                docs = extraccion.docs_de_json(datos)
            except extraccion.ErrorExtraccion as exc:
                raise ErrorCarga(str(exc)) from exc
            return [self._crear_json(doc, archivo, datos, opts, i, len(docs))
                    for i, doc in enumerate(docs, 1)]

        t = self._nuevo('archivo', opts)
        if raw_extra:
            t['original_json'] = dict(raw_extra)
        if avisos_origen:
            t['avisos_origen'] = list(avisos_origen)
        if doc_id is not None:
            t['doc_id'] = doc_id
        ext = Path(nombre).suffix.lower()
        t['archivo'] = dict(archivo, guardado=f'original{ext}')
        d = self._dir(t['id'])
        d.mkdir(parents=True, exist_ok=True)
        (d / f'original{ext}').write_bytes(datos)
        with self._lock:
            self._trabajos[t['id']] = t
            self._guardar(t)
        self._encolar('extraer', t['id'])
        return [t]

    def _crear_json(self, doc, archivo, datos, opts, i, n) -> dict:
        """Un documento de un JSON en el formato de raw/ (export de Paperless).
        Conserva su paperlessId: re-exportar desde Paperless lo reemplaza en
        vez de duplicarlo."""
        t = self._nuevo('json', opts)
        pid = doc.get('paperlessId') or doc.get('id')
        if pid is not None:
            try:
                doc_id = ids.doc_id_de(pid)
            except (TypeError, ValueError):
                raise ErrorCarga(f'El documento {i} trae un paperlessId '
                                 f'invalido: {pid!r}.')
            if ids.numero_de(doc_id) > ids.RANGO_BASE + ids.RANGO_MAX:
                raise ErrorCarga(f'El documento {i} trae el id {pid}, que es '
                                 'del organigrama de MOF (900.001+): esos no '
                                 'se cargan a mano.')
            t['doc_id'] = doc_id
        t['archivo'] = dict(archivo, guardado='original.json',
                            indice=i if n > 1 else None)
        t['original_json'] = {k: v for k, v in doc.items()
                              if k not in CAMPOS_EDITABLES}
        t['borrador'] = {
            'titulo': opts['titulo'] or (doc.get('titulo') or '').strip(),
            'tipo_documento': (opts['tipo_documento']
                               or (doc.get('tipo_documento') or '').strip()),
            'etiquetas': (opts['etiquetas'] if opts['etiquetas'] is not None
                          else _etiquetas(doc.get('etiquetas'))),
            'contenido': extraccion.normalizar(doc.get('contenido') or '').strip(),
            'nroPaginas': doc.get('nroPaginas'),
        }
        t['extraccion'] = {'metodo': 'json', 'paginas': doc.get('nroPaginas'),
                           'ocr': None, 'avisos': []}
        d = self._dir(t['id'])
        d.mkdir(parents=True, exist_ok=True)
        (d / 'original.json').write_text(
            json.dumps(doc, ensure_ascii=False, indent=1), encoding='utf-8')
        self._tras_extraer(t)
        return t

    def crear_desde_texto(self, titulo, contenido, tipo_documento=None,
                          etiquetas=None, revisar=True, enriquecer=True,
                          permitir_duplicado=False) -> dict:
        """Informacion escrita o pegada a mano (no hay archivo que leer)."""
        if not (contenido or '').strip():
            raise ErrorCarga('El texto esta vacio.')
        opts = self._opciones(revisar=revisar, enriquecer=enriquecer,
                              titulo=titulo, tipo_documento=tipo_documento,
                              etiquetas=etiquetas,
                              permitir_duplicado=permitir_duplicado)
        t = self._nuevo('texto', opts)
        contenido = extraccion.normalizar(contenido).strip()
        t['borrador'] = {
            'titulo': opts['titulo'] or '',
            'tipo_documento': opts['tipo_documento'] or 'Información',
            'etiquetas': opts['etiquetas'] or [],
            'contenido': contenido, 'nroPaginas': None,
        }
        t['extraccion'] = {'metodo': 'texto', 'paginas': None, 'ocr': None,
                           'avisos': []}
        self._dir(t['id']).mkdir(parents=True, exist_ok=True)
        self._tras_extraer(t)
        return t

    def _extraer(self, tid):
        t = self._trabajos.get(tid)
        if t is None or t['estado'] not in ('en_cola', 'extrayendo'):
            return
        with self._lock:
            t['estado'] = 'extrayendo'
            t['error'] = None
            t['progreso'] = {'fase': 'texto', 'hecho': 0, 'total': None}
            self._guardar(t)

        def avance(fase, hecho, total):
            t['progreso'] = {'fase': fase, 'hecho': hecho, 'total': total}

        d = self._dir(tid)
        try:
            r = extraccion.extraer(
                d / t['archivo']['guardado'], t['archivo']['tipo'],
                modo=t['opciones']['modo'], motor_ocr=self.motor_ocr,
                progreso=avance, paginas_max=t['opciones']['paginas_max'])
        except extraccion.ErrorExtraccion as exc:
            return self._fallo(t, 'extraccion', str(exc))
        except Exception as exc:
            return self._fallo(t, 'extraccion', f'{type(exc).__name__}: {exc}')

        if r.get('markdown'):
            (d / 'ocr.md').write_text(r['markdown'], encoding='utf-8')
        sugerido = revision.sugerir_titulo(r['contenido'])
        tipo_doc = t['opciones']['tipo_documento'] or TIPO_POR_DEFECTO
        titulo = (t['opciones']['titulo'] or sugerido
                  or Path(t['archivo']['nombre']).stem)
        organo = revision.identidad_del_encabezado(r['contenido'])
        etiquetas = t['opciones']['etiquetas']
        if etiquetas is None:
            etiquetas = (revision.organo_a_etiquetas(organo[0]) if organo
                         and revision.es_resolucion(tipo_doc) else [])
        t['borrador'] = {'titulo': titulo, 'tipo_documento': tipo_doc,
                         'etiquetas': etiquetas, 'contenido': r['contenido'],
                         'nroPaginas': r.get('paginas')}
        t['extraccion'] = {k: r.get(k) for k in
                           ('metodo', 'paginas', 'ocr', 'avisos')}
        t['extraccion']['markdown'] = bool(r.get('markdown'))
        self._tras_extraer(t)

    def _tras_extraer(self, t):
        """Revision del borrador y, si no hay que esperar a nadie, indexar."""
        t['revision'] = self._revisar(t['borrador'], t.get('doc_id'),
                                      t['opciones']['permitir_duplicado'],
                                      t.get('avisos_origen'))
        t['progreso'] = None
        directo = not t['opciones']['revisar'] and t['revision']['ok']
        with self._lock:
            t['estado'] = 'indexando' if directo else 'revision'
            self._trabajos[t['id']] = t
            self._guardar(t)
        if directo:
            self._encolar('indexar', t['id'])

    def _revisar(self, borrador, doc_id=None, permitir_duplicado=False,
                 avisos_origen=None):
        def buscar_huella(h):
            try:
                return self.ingestor.doc_con_huella(h)
            except Exception:
                return None
        rev = revision.revisar(
            dict(borrador), buscar_huella=buscar_huella,
            buscar_identidad=self._buscar_identidad, doc_id=doc_id,
            permitir_duplicado=permitir_duplicado)
        if avisos_origen:
            rev['avisos'] = list(avisos_origen) + list(rev.get('avisos') or [])
        return rev

    def trabajo_de_origen(self, clave, id_origen):
        """El id del ultimo trabajo que vino de otro sistema con ese id, o None.
        Es lo que evita duplicar si se perdio el registro de ese sistema."""
        with self._lock:
            for t in sorted(self._trabajos.values(), key=lambda x: x['id'],
                            reverse=True):
                extra = (t.get('original_json') or {}).get(clave)
                if isinstance(extra, dict) and extra.get('id') == id_origen:
                    return t['id']
        return None

    def _buscar_identidad(self, organo, num, anio):
        """Documentos con esa identidad: el corpus segun grafo_citas.json, y lo
        cargado a mano segun su titulo (todavia no esta en el grafo)."""
        encontrados = []
        ruta = self.raw.parent / 'grafo_citas.json'
        try:
            with open(ruta, encoding='utf-8') as f:
                docs = json.load(f).get('docs', {})
            for d, v in docs.items():
                if (str(v.get('org') or '').upper() == organo
                        and str(v.get('num') or '').lstrip('0') == str(num).lstrip('0')
                        and str(v.get('anio') or '') == str(anio)):
                    encontrados.append((d, self._titulo_raw(d)))
        except Exception:
            pass
        from gaceta_rag.mejoras import identidad
        for d in self._docs_gestionados():
            raw = self._leer_raw(d)
            if not raw:
                continue
            o, n = identidad.identidad_titulo(raw.get('titulo'), raw.get('contenido') or '')
            if o == organo and n == f'{int(num)}/{anio}' and d not in dict(encontrados):
                encontrados.append((d, raw.get('titulo')))
        return encontrados

    def confirmar(self, tid, titulo=None, contenido=None, tipo_documento=None,
                  etiquetas=None, permitir_duplicado=None, enriquecer=None) -> dict:
        """El usuario reviso el borrador (y quiza lo corrigio): a indexar."""
        with self._lock:
            t = self._trabajo_o_error(tid)
            if t['estado'] != 'revision' and not (
                    t['estado'] == 'error' and t.get('borrador')):
                raise Conflicto(f'El trabajo esta en estado "{t["estado"]}"; '
                                'solo se confirma uno en revision.')
            b = dict(t['borrador'])
            if titulo is not None:
                b['titulo'] = titulo.strip()
            if contenido is not None:
                b['contenido'] = extraccion.normalizar(contenido).strip()
            if tipo_documento is not None:
                b['tipo_documento'] = tipo_documento.strip()
            if etiquetas is not None:
                b['etiquetas'] = _etiquetas(etiquetas)
            if permitir_duplicado is not None:
                t['opciones']['permitir_duplicado'] = bool(permitir_duplicado)
            if enriquecer is not None:
                t['opciones']['enriquecer'] = bool(enriquecer)
            rev = self._revisar(b, t.get('doc_id'),
                                t['opciones']['permitir_duplicado'],
                                t.get('avisos_origen'))
            t['borrador'], t['revision'] = b, rev
            if not rev['ok']:
                self._guardar(t)
                raise RevisionConErrores(rev)
            t['estado'] = 'indexando'
            t['error'] = None
            self._guardar(t)
        self._encolar('indexar', tid)
        return t

    def _reservar_doc_id(self, t) -> str:
        """El doc_id se fija ANTES de escribir nada y queda guardado en el
        trabajo: si el proceso muere a mitad, al reintentar se usa el mismo y
        no queda un documento a medias con otro numero."""
        with self._lock:
            if t.get('doc_id'):
                return t['doc_id']
            reservados = [x['doc_id'] for x in self._trabajos.values()
                          if x.get('doc_id')]
            doc_id = ids.siguiente_doc_id(
                self.raw, reservados=reservados,
                ultimo_entregado=self._estado_disco.get('ultimo_doc_id'))
            t['doc_id'] = doc_id
            self._estado_disco['ultimo_doc_id'] = doc_id
            self._guardar_estado()
            self._guardar(t)
            return doc_id

    def _raw_de(self, t, doc_id) -> dict:
        b = t['borrador']
        raw = dict(t.get('original_json') or {})
        previo = self._leer_raw(doc_id) if t['origen'] == 'edicion' else None
        if previo:
            raw = {k: v for k, v in previo.items() if k not in CAMPOS_EDITABLES}
            raw.update(t.get('original_json') or {})
        raw.update({
            'contenido': b['contenido'],
            'paperlessId': ids.numero_de(doc_id),
            'titulo': b['titulo'],
            'tipo_documento': b.get('tipo_documento') or '',
            'etiquetas': b.get('etiquetas') or [],
        })
        if b.get('nroPaginas'):
            raw['nroPaginas'] = b['nroPaginas']
        carga_previa = (previo or {}).get('carga') or {}
        raw['carga'] = {
            'origen': t['origen'] if t['origen'] != 'edicion'
                      else carga_previa.get('origen', 'edicion'),
            'trabajo': (carga_previa.get('trabajo') if t['origen'] == 'edicion'
                        else t['id']),
            'archivo': ((t.get('archivo') or {}).get('nombre')
                        or carga_previa.get('archivo')),
            'sha256': ((t.get('archivo') or {}).get('sha256')
                       or carga_previa.get('sha256')),
            'metodo': ((t.get('extraccion') or {}).get('metodo')
                       if t['origen'] != 'edicion'
                       else carga_previa.get('metodo')),
            'ocr': ((t.get('extraccion') or {}).get('ocr')
                    if t['origen'] != 'edicion' else carga_previa.get('ocr')),
            'cargado': carga_previa.get('cargado') or _ahora(),
            'editado': _ahora() if t['origen'] == 'edicion' else None,
        }
        return raw

    def _candado_motor(self):
        return getattr(self.motor, '_lock', None) or contextlib.nullcontext()

    def _indexar(self, tid):
        t = self._trabajos.get(tid)
        if t is None or t['estado'] != 'indexando':
            return
        try:
            doc_id = self._reservar_doc_id(t)
            raw = self._raw_de(t, doc_id)
            t['resultado'] = self._indexar_raw(doc_id, raw)
        except Exception as exc:
            return self._fallo(t, 'indexado', f'{type(exc).__name__}: {exc}')
        with self._lock:
            t['estado'] = 'indexado'
            enriquecer = t['opciones']['enriquecer']
            t['grafo'] = ({'estado': 'pendiente'} if enriquecer
                          else {'estado': 'omitido', 'detalle': 'no se pidio'})
            self._guardar(t)
        if enriquecer:
            self._pedir_grafo(doc_id)

    def _indexar_raw(self, doc_id, raw) -> dict:
        """prepara (sin locks) -> indexa (con el lock del motor) -> persiste
        -> verifica. Devuelve el resultado con la verificacion."""
        prep = self.ingestor.preparar(doc_id, raw)
        if not prep['chunks']:
            raise ErrorCarga('el documento no genera fragmentos')
        with self._lock_indice, self._candado_motor():
            res = self.ingestor.indexar(prep, raw)
            self._despues_de_tocar(doc_id)
        persistido = self._persistir()
        verif = self.verificar(doc_id, prep)
        return dict(res, persistido=persistido, verificacion=verif)

    def _despues_de_tocar(self, doc_id):
        """Lo que el motor tiene cacheado de este documento ya no vale."""
        cache = getattr(self.motor, 'cache_titulos', None)
        if cache is not None and hasattr(cache, 'olvidar'):
            cache.olvidar(doc_id)
        n = ids.numero_de(doc_id)
        if (n is not None and n <= ids.RANGO_BASE
                and n > (getattr(self.motor, 'max_doc_id', 0) or 0)):
            self.motor.max_doc_id = n

    def _persistir(self):
        if not self.persistir_inmediato:
            return False
        try:
            return bool(self.ingestor.persistir().get('persistido'))
        except Exception as exc:
            print(f'[carga] no se pudo bajar el indice a disco: {exc}')
            return False

    def verificar(self, doc_id, prep=None) -> dict:
        """Comprueba que el documento quedo BIEN, no solo que no fallo.

          - las dos mitades de SQLite (chunks_meta y la tabla FTS) y los
            vectores vivos tienen la misma cantidad de fragmentos;
          - cada vector vivo es el de su texto actual (desfasados());
          - una busqueda densa con su propio primer fragmento lo trae primero.
            Si no, algo no cuadra (indice y metadata desalineados, vectores
            de otro modelo...) aunque los conteos den bien.
        """
        if prep is None:
            raw = self._leer_raw(doc_id) or {}
            prep = {'chunks': revision.fragmentos(raw.get('contenido') or '',
                                                  raw, doc_id)}
        res = self.ingestor.resumen_doc(doc_id)
        esperado = len(prep['chunks'])
        desfasado = doc_id in self.ingestor.desfasados([doc_id])
        problemas = []
        if not (res['chunks'] == res['chunks_fts'] == res['vectores_vivos'] == esperado):
            problemas.append(f"conteos distintos: {esperado} fragmentos, "
                             f"{res['chunks']} en SQLite, {res['chunks_fts']} en "
                             f"FTS, {res['vectores_vivos']} vectores")
        if desfasado:
            problemas.append('hay vectores que no corresponden al texto actual')
        puesto = None
        motor = self.motor
        if prep and prep['chunks'] and getattr(motor, 'conn', None) is not None:
            try:
                import consultar_gaceta as cg
                consulta = prep['chunks'][0]['texto_puro'][:400]
                with self._candado_motor():
                    hits = cg.buscar_faiss_v2(
                        consulta, motor.faiss_index, motor.faiss_meta,
                        motor.conn, motor.modelo_emb, motor.requiere_e5, top_k=10)
                orden = []
                for h in hits:
                    if h['doc_id'] not in orden:
                        orden.append(h['doc_id'])
                puesto = orden.index(doc_id) + 1 if doc_id in orden else None
                if puesto != 1:
                    problemas.append('la busqueda con su propio texto no lo trae '
                                     f'primero (puesto {puesto or "fuera del top 10"})')
            except Exception as exc:
                problemas.append(f'no se pudo probar la busqueda: {exc}')
        return dict(res, esperados=esperado, desfasado=desfasado,
                    puesto_autobusqueda=puesto, ok=not problemas,
                    problemas=problemas)

    def _pedir_grafo(self, doc_id):
        with self._lock:
            self._pendientes_grafo.add(doc_id)
            if self._grafo_encolado:
                return
            self._grafo_encolado = True
        self._encolar('grafo')

    def _marcar_grafo(self, doc_id, estado, detalle=None):
        with self._lock:
            for t in self._trabajos.values():
                if t.get('doc_id') == doc_id and t['estado'] == 'indexado':
                    t['grafo'] = {'estado': estado, 'detalle': detalle,
                                  'cuando': _ahora()}
                    self._guardar(t)

    def _enriquecer(self):
        """LLM sobre los documentos pendientes, despues los pasos derivados UNA
        vez para todos, y recarga de los indices del motor."""
        with self._lock:
            pendientes = sorted(self._pendientes_grafo)
            self._pendientes_grafo.clear()
            self._grafo_encolado = False
        if not pendientes:
            return
        llm = self._esperar_llm()
        if llm is None:
            for d in pendientes:
                self._marcar_grafo(d, 'error', 'el motor no tiene LLM cargado '
                                   '(¿falta la clave de la API?). El documento '
                                   'es buscable; el grafo se puede pedir despues.')
            self.ultimo_grafo = {'cuando': _ahora(), 'ok': False,
                                 'motivo': 'sin LLM', 'docs': pendientes}
            return
        nuevos, fallidos = {}, {}
        for d in pendientes:
            raw = self._leer_raw(d)
            if not raw:
                continue
            self._marcar_grafo(d, 'en_curso')
            j = grafo.extraer_hechos(llm, raw, candado=self._candado_motor())
            if '_error' in j:
                fallidos[d] = j['_error']
                self._marcar_grafo(d, 'error', j['_error'])
            else:
                nuevos[d] = j
        grafo.guardar_hechos(nuevos, self.ruta_grafo_llm)
        pasos = self._derivados() if nuevos else []
        for d in nuevos:
            self._marcar_grafo(d, 'ok', {'tema': nuevos[d].get('tema'),
                                         'personas': len(nuevos[d].get('personas') or []),
                                         'citas': len(nuevos[d].get('citas') or [])})
        self.ultimo_grafo = {'cuando': _ahora(), 'ok': not fallidos,
                             'docs': sorted(nuevos), 'fallidos': fallidos,
                             'pasos': pasos}

    def _esperar_llm(self, maximo=900):
        """El LLM del motor, esperando a que termine de cargar.

        La carga arranca apenas el indice esta listo, y el motor carga el LLM
        AL FINAL (motor_web.cargar). Al reiniciar con grafos pendientes, este
        carril corria en ese hueco y los marcaba "sin LLM" sin estar realmente
        sin LLM: lo encontro la prueba de punta a punta."""
        limite = time.time() + maximo
        while (getattr(self.motor, 'llm', None) is None
               and getattr(self.motor, 'loading', False)
               and time.time() < limite):
            time.sleep(2)
        return getattr(self.motor, 'llm', None)

    def _derivados(self) -> list:
        """Adyacencia, firmantes, enlaces de MOF; y recarga de los indices."""
        pasos = grafo.rehacer_derivados(ejecutar=self._ejecutar)
        recarga = self._recargar(self.motor)
        return [{'paso': p['paso'], 'ok': p['ok']} for p in pasos] + [
            {'paso': 'recarga de indices', 'ok': True, 'detalle': recarga}]

    def _estado_en_grafo(self, doc_id, en=None, raw=None) -> str:
        """'al_dia' | 'desactualizado' | 'pendiente' (ver grafo_llm.json).

        'desactualizado': la entrada es de un texto anterior (se edito despues
        de pasarlo por el LLM). Su tema y sus citas pueden ya no ser ciertos."""
        en = grafo.en_grafo(self.ruta_grafo_llm) if en is None else en
        hechos = en.get(doc_id)
        if not hechos or '_error' in hechos:
            return 'pendiente'
        raw = self._leer_raw(doc_id) if raw is None else raw
        actual = gb.hash_contenido((raw or {}).get('contenido') or '')
        return 'al_dia' if hechos.get('_hash') == actual else 'desactualizado'

    def pedir_grafo(self, doc_ids=None) -> list:
        """Encola el grafo para esos documentos, o para los cargados a mano
        que no estan en grafo_llm.json o estan con un texto anterior."""
        en = grafo.en_grafo(self.ruta_grafo_llm)
        gestionados = set(self._docs_gestionados())
        if doc_ids:
            faltan = [d for d in doc_ids if d not in gestionados]
            if faltan:
                raise NoEncontrado(f'No son documentos cargados a mano: {faltan}')
            objetivo = list(doc_ids)
        else:
            objetivo = sorted(d for d in gestionados
                              if self._estado_en_grafo(d, en) != 'al_dia')
        for d in objetivo:
            self._marcar_grafo(d, 'pendiente')
            self._pedir_grafo(d)
        return objetivo

    def _leer_raw(self, doc_id):
        try:
            with open(self.raw / f'{doc_id}.json', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return None

    def _titulo_raw(self, doc_id):
        raw = self._leer_raw(doc_id)
        return (raw or {}).get('titulo')

    def _docs_gestionados(self) -> list:
        """Los que se pueden editar y borrar desde aqui: el rango manual, y lo
        importado por esta via (un JSON de Paperless con su propio id)."""
        docs = set()
        if self.raw.is_dir():
            docs |= {p.stem for p in self.raw.glob('doc_*.json')
                     if ids.es_carga_manual(p.stem)}
        with self._lock:
            docs |= {t['doc_id'] for t in self._trabajos.values()
                     if t.get('doc_id') and t['estado'] == 'indexado'
                     and (self.raw / f"{t['doc_id']}.json").exists()}
        return sorted(docs)

    def _gestionado_o_error(self, doc_id):
        if doc_id not in self._docs_gestionados():
            if (self.raw / f'{doc_id}.json').exists():
                raise Conflicto(f'{doc_id} es del corpus de Paperless: no se '
                                'edita ni se borra desde la carga manual.')
            raise NoEncontrado(f'No existe el documento {doc_id}.')

    def documentos(self) -> list:
        en = grafo.en_grafo(self.ruta_grafo_llm)
        salida = []
        for d in self._docs_gestionados():
            raw = self._leer_raw(d) or {}
            carga = raw.get('carga') or {}
            salida.append({
                'doc_id': d, 'titulo': raw.get('titulo'),
                'tipo_documento': raw.get('tipo_documento'),
                'etiquetas': raw.get('etiquetas') or [],
                'caracteres': len(raw.get('contenido') or ''),
                'paginas': raw.get('nroPaginas'),
                'origen': carga.get('origen'), 'archivo': carga.get('archivo'),
                'metodo': carga.get('metodo'), 'cargado': carga.get('cargado'),
                'editado': carga.get('editado'),
                'grafo': self._estado_en_grafo(d, en, raw),
                'en_grafo': self._estado_en_grafo(d, en, raw) == 'al_dia',
                'tema': (en.get(d) or {}).get('tema'),
            })
        return salida

    def documento(self, doc_id) -> dict:
        self._gestionado_o_error(doc_id)
        raw = self._leer_raw(doc_id) or {}
        en = grafo.en_grafo(self.ruta_grafo_llm)
        trabajo = (raw.get('carga') or {}).get('trabajo')
        return {'doc_id': doc_id, 'raw': raw,
                'indice': self.ingestor.resumen_doc(doc_id),
                'grafo': en.get(doc_id), 'trabajo': trabajo,
                'original': self.ruta_original(doc_id) is not None}

    def ruta_original(self, doc_id):
        """El archivo subido de un documento, si se conserva."""
        raw = self._leer_raw(doc_id) or {}
        tid = (raw.get('carga') or {}).get('trabajo')
        t = self._trabajos.get(tid) if tid else None
        if not t or not t.get('archivo'):
            return None
        ruta = self._dir(tid) / t['archivo']['guardado']
        return ruta if ruta.exists() else None

    def actualizar(self, doc_id, titulo=None, contenido=None,
                   tipo_documento=None, etiquetas=None, enriquecer=True,
                   permitir_duplicado=False, raw_extra=None) -> dict:
        """Corrige un documento ya cargado (p. ej. un error del OCR) y lo
        reindexa. Pasa por la misma revision que una carga nueva."""
        self._gestionado_o_error(doc_id)
        raw = self._leer_raw(doc_id) or {}
        t = self._nuevo('edicion', self._opciones(
            revisar=False, enriquecer=enriquecer,
            permitir_duplicado=permitir_duplicado))
        t['doc_id'] = doc_id
        if raw_extra:
            t['original_json'] = dict(raw_extra)
        t['borrador'] = {
            'titulo': raw.get('titulo') or '',
            'tipo_documento': raw.get('tipo_documento') or '',
            'etiquetas': raw.get('etiquetas') or [],
            'contenido': raw.get('contenido') or '',
            'nroPaginas': raw.get('nroPaginas'),
        }
        t['estado'] = 'revision'
        self._dir(t['id']).mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._trabajos[t['id']] = t
            self._guardar(t)
        try:
            return self.confirmar(t['id'], titulo=titulo, contenido=contenido,
                                  tipo_documento=tipo_documento,
                                  etiquetas=etiquetas)
        except RevisionConErrores:
            with self._lock:
                self._trabajos.pop(t['id'], None)
            shutil.rmtree(self._dir(t['id']), ignore_errors=True)
            raise

    def eliminar(self, doc_id) -> dict:
        """Saca el documento de la base, del grafo y del disco (con su
        original). Las aristas que apuntaban a el se rehacen en segundo plano."""
        self._gestionado_o_error(doc_id)
        with self._lock_indice, self._candado_motor():
            res = self.ingestor.retirar(doc_id)
            self._despues_de_tocar(doc_id)
        estaba_en_grafo = grafo.quitar([doc_id], self.ruta_grafo_llm) > 0
        persistido = self._persistir()
        with self._lock:
            for t in list(self._trabajos.values()):
                if t.get('doc_id') == doc_id:
                    self._trabajos.pop(t['id'], None)
                    shutil.rmtree(self._dir(t['id']), ignore_errors=True)
        if estaba_en_grafo:
            self._encolar('derivados', f'se elimino {doc_id}')
        return dict(res, persistido=persistido, grafo=estaba_en_grafo)

    def _trabajo_o_error(self, tid) -> dict:
        t = self._trabajos.get(tid)
        if t is None:
            raise NoEncontrado(f'No existe el trabajo {tid}.')
        return t

    def trabajo(self, tid) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._trabajo_o_error(tid)))

    def trabajos(self) -> list:
        """Resumen de todos (sin el texto, que puede pesar megas)."""
        with self._lock:
            salida = []
            for t in sorted(self._trabajos.values(), key=lambda x: x['id'],
                            reverse=True):
                r = {k: t.get(k) for k in ('id', 'origen', 'estado', 'creado',
                                           'actualizado', 'doc_id', 'progreso',
                                           'error', 'grafo')}
                r['archivo'] = (t.get('archivo') or {}).get('nombre')
                b = t.get('borrador') or {}
                r['titulo'] = b.get('titulo')
                rev = t.get('revision') or {}
                r['errores'] = len(rev.get('errores') or [])
                r['avisos'] = len(rev.get('avisos') or [])
                r['verificado'] = ((t.get('resultado') or {}).get('verificacion')
                                   or {}).get('ok')
                salida.append(r)
            return salida

    def markdown_ocr(self, tid):
        ruta = self._dir(tid) / 'ocr.md'
        self._trabajo_o_error(tid)
        return ruta.read_text(encoding='utf-8') if ruta.exists() else None

    def descartar(self, tid) -> dict:
        with self._lock:
            t = self._trabajo_o_error(tid)
            if t['estado'] in ('extrayendo', 'indexando'):
                raise Conflicto('El trabajo se esta procesando; espera a que '
                                'termine.')
            if t['estado'] == 'indexado':
                raise Conflicto(f'Ya esta indexado como {t["doc_id"]}: para '
                                'sacarlo de la base, elimina el documento.')
            self._trabajos.pop(tid, None)
        shutil.rmtree(self._dir(tid), ignore_errors=True)
        return {'descartado': tid}

    def reintentar(self, tid) -> dict:
        with self._lock:
            t = self._trabajo_o_error(tid)
            if t['estado'] != 'error':
                raise Conflicto('Solo se reintenta un trabajo con error.')
            fase = (t.get('error') or {}).get('fase')
            if fase == 'extraccion':
                t['estado'], accion = 'en_cola', 'extraer'
            else:
                t['estado'], accion = 'indexando', 'indexar'
            t['error'] = None
            self._guardar(t)
        self._encolar(accion, tid)
        return t

    def _fallo(self, t, fase, mensaje):
        with self._lock:
            t['estado'] = 'error'
            t['progreso'] = None
            t['error'] = {'fase': fase, 'mensaje': mensaje, 'cuando': _ahora()}
            self._guardar(t)
        print(f"[carga] {t['id']}: error en {fase}: {mensaje}")

    def estado(self) -> dict:
        with self._lock:
            por_estado = {}
            for t in self._trabajos.values():
                por_estado[t['estado']] = por_estado.get(t['estado'], 0) + 1
            pendientes_grafo = sorted(self._pendientes_grafo)
        ocr = (self.motor_ocr.estado() if self.motor_ocr is not None
               else {'disponible': False, 'tipo': None,
                     'error': 'OCR no configurado (OCR_URL u OCR_LOCAL=1): los '
                              'PDF con texto se cargan, los escaneados no.'})
        llm = getattr(self.motor, 'llm', None)
        return {
            'listo': self.listo,
            'motor_disponible': bool(self.ingestor.disponible),
            'ocr': ocr,
            'llm': (f'{getattr(llm, "proveedor", "local")} / '
                    f'{getattr(llm, "modelo", "Qwen")}' if llm else None),
            'cola': sum(c.qsize() for c in self._colas.values()),
            'trabajos': por_estado,
            'documentos': len(self._docs_gestionados()),
            'grafo_pendiente': pendientes_grafo,
            'ultimo_grafo': self.ultimo_grafo,
            'reparacion': self.reparacion,
            'max_mb': MAX_MB,
            'extensiones': extraccion.EXTENSIONES,
        }

    def _recuperar(self):
        """Retoma lo que quedo a medias y repara los documentos desfasados."""
        with self._lock:
            trabajos = list(self._trabajos.values())
        for t in trabajos:
            if t['estado'] in ('en_cola', 'extrayendo'):
                t['estado'] = 'en_cola'
                self._encolar('extraer', t['id'])
            elif t['estado'] == 'indexando':
                self._encolar('indexar', t['id'])
            elif (t['estado'] == 'indexado'
                  and (t.get('grafo') or {}).get('estado') in ('pendiente', 'en_curso')):
                self._pedir_grafo(t['doc_id'])
        self.reparacion = self.reparar()

    def reparar(self) -> dict:
        """Cada documento cargado a mano tiene que estar en la base con SU
        texto: huella de docs_meta igual a la del raw, y vectores vivos iguales
        a sus chunks. Si el proceso murio entre escribir el raw y bajar el
        indice a disco, aqui se nota y se reindexa."""
        reindexados, errores = [], {}
        en_curso = {t.get('doc_id') for t in self._trabajos.values()
                    if t['estado'] == 'indexando'}
        docs = [d for d in self._docs_gestionados() if d not in en_curso]
        desfasados = self.ingestor.desfasados(docs) if docs else set()
        for d in docs:
            raw = self._leer_raw(d)
            if not raw:
                continue
            huella = gb.hash_contenido(raw.get('contenido') or '')
            if d in desfasados or self.ingestor.huella_de(d) != huella:
                try:
                    self._indexar_raw(d, raw)
                    reindexados.append(d)
                except Exception as exc:
                    errores[d] = f'{type(exc).__name__}: {exc}'
        if reindexados or errores:
            print(f'[carga] reparacion: {len(reindexados)} reindexado(s), '
                  f'{len(errores)} con error')
        return {'cuando': _ahora(), 'revisados': len(docs),
                'reindexados': reindexados, 'errores': errores}
