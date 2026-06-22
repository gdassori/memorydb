"""CodeAdapter — multilang symbol/edge extraction via tree-sitter (TD-005). STUB.

Lands with the ``[code]`` extra (tree-sitter + tree-sitter-language-pack). It will:
  * extract nodes (function/class/method/import) uniformly across languages,
  * emit COARSE, name-based edges tagged with ``confidence < 1.0`` (precise per-language
    resolvers arrive later as higher-confidence Extractors),
  * serialize each node's neighborhood for graph-aware embedding (TD-006).
See docs/specs/active/v0-substrate.md (pending tasks).
"""
from __future__ import annotations


class CodeAdapter:  # pragma: no cover - stub
    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError(
            "CodeAdapter needs the [code] extra (tree-sitter, tree-sitter-language-pack). "
            "See TD-005 and docs/specs/active/v0-substrate.md."
        )

    def extract(self, path: str):
        raise NotImplementedError
