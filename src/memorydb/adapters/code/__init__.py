"""CodeAdapter — multilang symbol & coarse-edge extraction via tree-sitter (TD-005).

Implements the Extractor port: ``extract(path) -> Extraction`` (nodes, in-file edges, and *pending*
edges to be resolved globally by the indexer — C2). Requires the ``[code]`` extra
(tree-sitter + tree-sitter-language-pack). The canonical ``tree_sitter.Parser(get_language(...))``
API is used (the language-pack's own ``get_parser`` ships a broken parser binding).
"""
from __future__ import annotations

import os
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from memorydb.models import Edge, Node, Rel


class LangSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str                       # tree-sitter-language-pack grammar name
    extensions: tuple[str, ...]
    func_types: frozenset[str]
    class_types: frozenset[str]
    call_types: frozenset[str]
    import_types: frozenset[str]
    id_types: frozenset[str]         # identifier-like node types used for name extraction


_ID_C = frozenset({"identifier", "dotted_name", "field_identifier", "type_identifier",
                   "property_identifier", "scoped_identifier", "package_identifier"})

LANGS: tuple = (
    LangSpec(name="python", extensions=(".py",),
             func_types=frozenset({"function_definition"}), class_types=frozenset({"class_definition"}),
             call_types=frozenset({"call"}),
             import_types=frozenset({"import_statement", "import_from_statement"}), id_types=_ID_C),
    LangSpec(name="javascript", extensions=(".js", ".jsx", ".mjs", ".cjs"),
             func_types=frozenset({"function_declaration", "method_definition", "generator_function_declaration"}),
             class_types=frozenset({"class_declaration"}),
             call_types=frozenset({"call_expression", "new_expression"}),
             import_types=frozenset({"import_statement"}), id_types=_ID_C),
    LangSpec(name="typescript", extensions=(".ts", ".tsx"),
             func_types=frozenset({"function_declaration", "method_definition"}),
             class_types=frozenset({"class_declaration", "interface_declaration"}),
             call_types=frozenset({"call_expression", "new_expression"}),
             import_types=frozenset({"import_statement"}), id_types=_ID_C),
    LangSpec(name="go", extensions=(".go",),
             func_types=frozenset({"function_declaration", "method_declaration"}),
             class_types=frozenset({"type_declaration"}),
             call_types=frozenset({"call_expression"}),
             import_types=frozenset({"import_declaration"}), id_types=_ID_C),
    LangSpec(name="rust", extensions=(".rs",),
             func_types=frozenset({"function_item"}),
             class_types=frozenset({"struct_item", "enum_item", "trait_item", "impl_item"}),
             call_types=frozenset({"call_expression", "macro_invocation"}),
             import_types=frozenset({"use_declaration"}), id_types=_ID_C),
)
_BY_EXT = {ext: spec for spec in LANGS for ext in spec.extensions}

_MAX_WALK_DEPTH = 200  # AST nesting cap: stops a hostile deeply-nested file from blowing the recursion
                       # limit and aborting the whole index (security I1). Far beyond real code nesting.


class Extraction(BaseModel):
    nodes: list = Field(default_factory=list)            # list[Node]
    edges: list = Field(default_factory=list)            # list[Edge], in-file, dst is a uid, conf ~0.9
    pending: list = Field(default_factory=list)          # (src_uid, dst_name, relation, confidence) — C2


class LanguageRegistry:
    def spec_for(self, path: str) -> Optional[LangSpec]:
        return _BY_EXT.get(os.path.splitext(path)[1].lower())


class CodeAdapter:
    """Extractor port. Coarse, name-based edges tagged with confidence < 1.0 (TD-005);
    precise edges arrive later from python-precise-resolver and supersede these via MAX-confidence upsert."""

    def __init__(self, registry: Optional[LanguageRegistry] = None, repo_root: str = ".") -> None:
        try:
            from tree_sitter import Parser  # noqa: F401
            from tree_sitter_language_pack import get_language  # noqa: F401
        except Exception as e:  # pragma: no cover
            raise NotImplementedError(
                "CodeAdapter needs the [code] extra: pip install -e '.[code]' "
                "(tree-sitter + tree-sitter-language-pack)."
            ) from e
        self.registry = registry or LanguageRegistry()
        self.repo_root = repo_root
        self._parsers: dict = {}

    # --- public ------------------------------------------------------------
    def handles(self, path: str) -> bool:
        return self.registry.spec_for(path) is not None

    def lang_of(self, path: str) -> Optional[str]:
        spec = self.registry.spec_for(path)
        return spec.name if spec else None

    def extract(self, path: str, data: Optional[bytes] = None) -> Extraction:
        spec = self.registry.spec_for(path)
        if spec is None:
            return Extraction()
        rel = os.path.relpath(path, self.repo_root).replace(os.sep, "/")
        if data is None:                       # reuse the indexer's already-read bytes when given (MR-15)
            with open(path, "rb") as fh:
                data = fh.read()
        # Guard the whole parse+extract: a hostile or pathological file (e.g. a deeply nested AST that
        # blows Python's recursion limit) must never abort the index run (security I1). _extract_tree
        # also depth-caps its own walk.
        try:
            tree = self._parser(spec.name).parse(data)
            return self._extract_tree(tree.root_node, data, rel, spec)
        except Exception:
            return Extraction()

    # --- parsing -----------------------------------------------------------
    def _parser(self, lang: str):
        if lang not in self._parsers:
            from tree_sitter import Parser
            from tree_sitter_language_pack import get_language
            self._parsers[lang] = Parser(get_language(lang))
        return self._parsers[lang]

    # --- extraction --------------------------------------------------------
    def _extract_tree(self, root, src: bytes, rel: str, spec: LangSpec) -> Extraction:
        nodes: list = []
        local: dict = {}            # simple name -> [uid, ...] (defs of that name in this file)
        imports: set = set()
        refs: list = []             # (enclosing_uid, callee_name, callee_root, relation)
        seen: set = set()

        def uid_for(qual: str, start_byte: int) -> str:
            u = f"{rel}::{qual}"
            if u in seen:           # deterministic disambiguation by byte offset (stable across re-parse)
                u = f"{u}#{start_byte}"
            seen.add(u)
            return u

        def walk(node, stack, enclosing, depth=0):
            if depth > _MAX_WALK_DEPTH:     # bound hostile/pathological nesting (security I1)
                return
            for child in node.named_children:
                t = child.type
                if t in spec.func_types or t in spec.class_types:
                    name = self._name(child, spec)
                    if not name:
                        walk(child, stack, enclosing, depth + 1)
                        continue
                    qual = ".".join(stack + [name])
                    u = uid_for(qual, child.start_byte)
                    kind = "class" if t in spec.class_types else ("method" if stack else "function")
                    nodes.append(Node(
                        uid=u, type=kind, name=name, body=self._text(child, src)[:2000],
                        attrs={"lang": spec.name, "file_uid": rel,
                               "signature": self._signature(child, src),
                               "docstring": self._docstring(child, src, spec),
                               "start_line": child.start_point[0] + 1,
                               "end_line": child.end_point[0] + 1},
                    ))
                    local.setdefault(name, []).append(u)
                    if t in spec.class_types:
                        for base in self._base_names(child, src, spec):
                            refs.append((u, base, base, Rel.INHERITS))
                    walk(child, stack + [name], u, depth + 1)
                elif t in spec.import_types:
                    imports.update(self._import_names(child, src, spec))
                    walk(child, stack, enclosing, depth + 1)
                elif t in spec.call_types:
                    name, rootname = self._callee(child, spec, src)
                    if name and enclosing:
                        refs.append((enclosing, name, rootname, Rel.CALLS))
                    walk(child, stack, enclosing, depth + 1)
                else:
                    walk(child, stack, enclosing, depth + 1)

        walk(root, [], None)

        edges: list = []
        pending: list = []
        for src_uid, name, rootname, relation in refs:
            cands = local.get(name, [])
            if len(cands) == 1:                                 # exactly one same-file def: precise-ish
                edges.append(Edge(src=src_uid, dst=cands[0], relation=relation,
                                  confidence=0.9, source="treesitter"))
            elif name in imports or rootname in imports:        # import-scoped
                pending.append((src_uid, name, relation, 0.6))
            else:
                # bare global name, OR an ambiguous same-file name (e.g. a method name on two classes).
                # Never emit a 0.9 edge to an arbitrarily-chosen def (correctness I6); resolve globally
                # at low confidence and let the precise resolver settle it.
                pending.append((src_uid, name, relation, 0.3))
        return Extraction(nodes=nodes, edges=edges, pending=pending)

    # --- node helpers ------------------------------------------------------
    @staticmethod
    def _text(node, src: bytes) -> str:
        return src[node.start_byte:node.end_byte].decode("utf-8", "replace")

    def _name(self, node, spec: LangSpec) -> Optional[str]:
        nm = node.child_by_field_name("name")
        if nm is not None:
            return nm.text.decode("utf-8", "replace").split(".")[-1]
        return self._first_id(node, spec)

    def _first_id(self, node, spec: LangSpec) -> Optional[str]:
        for child in node.named_children:
            if child.type in spec.id_types:
                return child.text.decode("utf-8", "replace").split(".")[-1]
        return None

    @staticmethod
    def _signature(node, src: bytes) -> str:
        return src[node.start_byte:node.end_byte].decode("utf-8", "replace").splitlines()[0].strip()

    def _docstring(self, node, src: bytes, spec: LangSpec) -> str:
        if spec.name != "python":
            return ""
        body = node.child_by_field_name("body")
        if body is None or not body.named_children:
            return ""
        first = body.named_children[0]
        # Newer tree-sitter-python puts the docstring as a bare `string` node in the block;
        # older grammars wrap it in an `expression_statement`. Handle both.
        s = None
        if first.type == "string":
            s = first
        elif first.type == "expression_statement" and first.named_children and \
                first.named_children[0].type == "string":
            s = first.named_children[0]
        if s is None:
            return ""
        content = next((c.text.decode("utf-8", "replace")
                        for c in s.named_children if c.type == "string_content"), None)
        if content is None:  # no string_content child: strip the quotes off the whole literal
            content = s.text.decode("utf-8", "replace").strip().strip('"').strip("'")
        content = content.strip()
        return content.splitlines()[0] if content else ""

    def _base_names(self, node, src: bytes, spec: LangSpec) -> list:
        supers = node.child_by_field_name("superclasses")
        if supers is None:
            return []
        return [c.text.decode("utf-8", "replace").split(".")[-1]
                for c in supers.named_children if c.type in spec.id_types]

    def _import_names(self, node, src: bytes, spec: LangSpec) -> set:
        out: set = set()
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type in spec.id_types:
                txt = n.text.decode("utf-8", "replace")
                out.add(txt)
                out.add(txt.split(".")[-1])
            stack.extend(n.named_children)
        return out

    def _callee(self, call_node, spec: LangSpec, src: bytes):
        """Return (callee_name, callee_root) from a call node's function/callee subtree."""
        fn = call_node.child_by_field_name("function") or (
            call_node.named_children[0] if call_node.named_children else None)
        if fn is None:
            return None, None
        ids = []
        stack = [fn]
        # collect identifier-like leaves in source order (approx via DFS then sort by byte)
        leaves = []
        while stack:
            n = stack.pop()
            if n.type in spec.id_types and not n.named_children:
                leaves.append(n)
            stack.extend(n.named_children)
        if not leaves:
            # the function field itself may be a bare identifier
            if fn.type in spec.id_types:
                t = fn.text.decode("utf-8", "replace")
                return t.split(".")[-1], t.split(".")[0]
            return None, None
        leaves.sort(key=lambda n: n.start_byte)
        texts = [n.text.decode("utf-8", "replace") for n in leaves]
        return texts[-1], texts[0]
