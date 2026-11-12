"""
Modulo de mejoras para el sistema RAG Gaceta UMSA.

Contiene:
  - reranker.py            Cross-encoder BGE para re-ranquear candidatos
  - mmr.py                 Maximal Marginal Relevance (diversidad)
  - eval.py                Metricas de evaluacion (recall, MRR, hit@1)
  - prompts_mejorados.py   Prompt template optimizado para Qwen 2.5
  - descargar_reranker.py  Descarga el modelo BGE reranker
  - retriever_mejorado.py  Retriever que integra todo

Sin destruir el sistema original. Cada modulo es opcional.
"""
