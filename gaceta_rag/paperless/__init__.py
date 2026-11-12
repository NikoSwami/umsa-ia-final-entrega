# -*- coding: utf-8 -*-
"""
Integracion con Paperless-ngx: la puerta de entrada de documentos por MCP.

El MCP solo habla con Paperless; de ahi a la Gaceta los trae este paquete, que
los entrega a la carga manual (gaceta_rag/carga). Ver PLAN_PAPERLESS_MCP.md.

  cliente   lee la API REST de Paperless. Nunca escribe.
"""
