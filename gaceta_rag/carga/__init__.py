# -*- coding: utf-8 -*-
"""
Carga MANUAL de documentos: un PDF, una imagen, un texto o un JSON de
Paperless entran a la base desde la interfaz /carga, sin reconstruir nada.

Hasta ahora la unica puerta era dejar JSON en data/raw/ y correr
construir_todo.py. Este paquete agrega la otra: subir el archivo, revisar lo
que se extrajo y confirmarlo. El documento queda buscable en el chat en
segundos, por el mismo camino que la ingesta en caliente de MOF.

  ids.py         rango reservado de doc_id para lo que se carga a mano
                 (doc_100001...), y por que no puede ser el de Paperless.
  a_markdown.py  salida de PaddleOCR-VL -> Markdown puro (tablas de pipes).
                 Viene del proyecto orc-con-ia-desde-google-colab.
  ocr.py         los dos motores de OCR: PaddleOCR-VL en este proceso, o el
                 servicio OCR aparte (servicio_ocr/, el contenedor "ocr").
  extraccion.py  archivo -> texto: capa de texto del PDF, OCR, .txt/.md, JSON.
  revision.py    lo que se revisa ANTES de indexar: titulo e identidad,
                 duplicados, calidad del OCR, limites del pipeline.
  grafo.py       despues de indexar: pasar el documento por el LLM (tema,
                 personas, citas) y rehacer grafo/indices sin GPU.
  cargador.py    el orquestador: trabajos, cola, estados, reparacion al
                 arrancar.

Nada de esto se importa si nadie carga documentos: sin uso, el motor se
comporta exactamente igual que antes.
"""
