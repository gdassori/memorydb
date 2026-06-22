"""Adapters map a domain onto the generic substrate (TD-002).

Each adapter implements the ``Extractor`` port and decides how to serialize a node's
neighborhood for graph-aware embeddings (TD-006). The substrate core never imports adapters.
"""
