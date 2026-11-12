# -*- coding: utf-8 -*-
"""
Integracion con MOF (Manual de Organizacion y Funciones).

MOF es la app externa (NestJS + PostgreSQL) que guarda el organigrama de la
universidad: unidades, sus funciones, cargos y personas asignadas. Este
paquete lo conecta con la base de conocimiento de la gaceta por dos vias:

  exportador.py  unidad de MOF -> ficha de texto con el MISMO esquema que un
                 raw doc (data/raw/doc_NNNNN.json), para que el pipeline la
                 indexe sin cambios.
  ingesta.py     aplica UN evento de MOF (alta/cambio/baja de una unidad)
                 sobre la base ya construida, en caliente, sin reconstruir
                 nada mas.
  enlaces.py     cruza la 'base legal' de cada unidad con las resoluciones del
                 corpus y mete esas aristas en el grafo de citas.
  cliente.py     cliente HTTP minimo contra la API REST de MOF.
  respaldo.py    llena la replica desde un RESPALDO de la base de MOF (un
                 pg_dump en texto plano), para cuando MOF no es alcanzable
                 desde aqui. Reproduce lo que habria publicado su API: no
                 entra lo dado de baja, las etiquetas salen de los catalogos y
                 la fecha de los datos es la del respaldo.
  intencion.py   reconoce, sin LLM, que pregunta una consulta sobre el
                 organigrama (contar, listar, jerarquia, funciones...) y que
                 unidad nombra, leyendo la replica.
  respuestas.py  arma, sin LLM, la respuesta exacta de contar, listar y la
                 jerarquia desde la replica (PASO 0c del chat), y la de
                 respaldo con los hechos de una unidad cuando hay que
                 descartar lo que redacto el modelo.
  hechos.py      los hechos verificados de una unidad (funciones, cargos,
                 creacion...): el bloque que el modelo recibe junto a la
                 pregunta con NOTA_ORGANIGRAMA, y la cuenta de que numeros de
                 la respuesta no salieron de ahi.
  herramientas.py  cinco consultas de SOLO LECTURA a la replica (contar,
                 hijas, rama, funciones, buscar) para el motor por API con
                 GACETA_API_HERRAMIENTAS=1.
  vigencia.py    de cuando son los datos que contesta el chat, segun la
                 ultima consulta a MOF que salio bien: "Datos de MOF al
                 <fecha>" y, si hace mas de 10 minutos o fue sin auditoria,
                 "(podría no reflejar cambios recientes)".

Todo es opcional y con contrato seguro: sin datos de MOF, nada de esto se
activa y el motor se comporta exactamente igual que antes.
"""
