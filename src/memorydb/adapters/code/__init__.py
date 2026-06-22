"""CodeAdapter — multilang symbol & coarse-edge extraction via tree-sitter (TD-005).

Implements the Extractor port: ``extract(path) -> Extraction`` (nodes, in-file edges, and *pending*
edges to be resolved globally by the indexer — C2). Requires the ``[code]`` extra
(tree-sitter + tree-sitter-language-pack). The canonical ``tree_sitter.Parser(get_language(...))``
API is used (the language-pack's own ``get_parser`` ships a broken parser binding).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from memorydb.models import Edge, Node, Rel


@dataclass(frozen=True)
class LangSpec:
    name: str                    # tree-sitter-language-pack grammar name
    extensions: tuple
    func_types: frozenset
    class_types: frozenset
    call_types: frozenset
    import_types: frozenset
    id_types: frozenset          # identifier-like node types used for name extraction


_ID_C = frozenset({"identifier", "dotted_name", "field_identifier", "type_identifier",
                   "property_identifier", "scoped_identifier", "package_identifier"})

LANGS: tuple = (
    LangSpec("python", (".py",),
             frozenset({"function_definition"}), frozenset({"class_definition"}),
             frozenset({"call"}), frozenset({"import_statement", "import_from_statement"}), _ID_C),
    LangSpec("javascript", (".js", ".jsx", ".mjs", ".cjs"),
             frozenset({"function_declaration", "method_definition", "generator_function_declaration"}),
             frozenset({"class_declaration"}),
             frozenset({"call_expression", "new_expression"}), frozenset({"import_statement"}), _ID_C),
    LangSpec("typescript", (".ts", ".tsx"),
             frozenset({"function_declaration", "method_definition"}),
             frozenset({"class_declaration", "interface_declaration"}),
             frozenset({"call_expression", "new_expression"}), frozenset({"import_statement"}), _ID_C),
    LangSpec("go", (".go",),
             frozenset({"function_declaration", "method_declaration"}),
             frozenset({"type_declaration"}),
             frozenset({"call_expression"}), frozenset({"import_declaration"}), _ID_C),
    LangSpec("rust", (".rs",),
             frozenset({"function_item"}),
             frozenset({"struct_item", "enum_item", "trait_item", "impl_item"}),
             frozenset({"call_expression", "macro_invocation"}), frozenset({"use_declaration"}), _ID_C),
)
_BY_EXT = {ext: spec for spec in LANGS for ext in spec.extensions}


@dataclass
class Extraction:
    nodes: list = field(default_factory=list)            # list[Node]
    edges: list = field(default_factory=list)            # list[Edge], in-file, dst is a uid, conf ~0.9
    pending: list = field(default_factory=list)          # (src_uid, dst_name, relation, confidence) — C2


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

    def extract(self, path: str) -> Extraction:
        spec = self.registry.spec_for(path)
        if spec is None:
            return Extraction()
        rel = os.path.relpath(path, self.repo_root).replace(os.sep, "/")
        with open(path, "rb") as fh:
            data = fh.read()
        try:
            tree = self._parser(spec.name).parse(data)
        except Exception:
            return Extraction()  # never fail the index on a parse error
        return self._extract_tree(tree.root_node, data, rel, spec)

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
        local: dict = {}            # simple name -> uid (defined in this file)
        imports: set = set()
        refs: list = []             # (enclosing_uid, callee_name, callee_root, relation)
        seen: set = set()

        def uid_for(qual: str, start_byte: int) -> str:
            u = f"{rel}::{qual}"
            if u in seen:           # deterministic disambiguation by byte offset (stable across re-parse)
                u = f"{u}#{start_byte}"
            seen.add(u)
            return u

        def walk(node, stack, enclosing):
            for child in node.named_children:
                t = child.type
                if t in spec.func_types or t in spec.class_types:
                    name = self._name(child, spec)
                    if not name:
                        walk(child, stack, enclosing)
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
                    local.setdefault(name, u)
                    if t in spec.class_types:
                        for base in self._base_names(child, src, spec):
                            refs.append((u, base, base, Rel.INHERITS))
                    walk(child, stack + [name], u)
                elif t in spec.import_types:
                    imports.update(self._import_names(child, src, spec))
                    walk(child, stack, enclosing)
                elif t in spec.call_types:
                    name, rootname = self._callee(child, spec, src)
                    if name and enclosing:
                        refs.append((enclosing, name, rootname, Rel.CALLS))
                    walk(child, stack, enclosing)
                else:
                    walk(child, stack, enclosing)

        walk(root, [], None)

        edges: list = []
        pending: list = []
        for src_uid, name, rootname, relation in refs:
            if name in local:                                   # same-file: precise-ish
                edges.append(Edge(src_uid, local[name], relation, confidence=0.9, source="treesitter"))
            elif name in imports or rootname in imports:        # import-scoped
                pending.append((src_uid, name, relation, 0.6))
            else:                                               # bare global name
                pending.append((src_uid, name, relation, 0.3))
        return Extraction(nodes, edges, pending)

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
