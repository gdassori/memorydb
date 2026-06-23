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
             func_types=frozenset({"function_declaration", "method_definition", "method_signature",
                                   "abstract_method_signature", "function_signature"}),
             class_types=frozenset({"class_declaration", "interface_declaration",
                                    "abstract_class_declaration", "enum_declaration"}),
             call_types=frozenset({"call_expression", "new_expression"}),
             import_types=frozenset({"import_statement"}), id_types=_ID_C),
    LangSpec(name="go", extensions=(".go",),
             func_types=frozenset({"function_declaration", "method_declaration"}),
             class_types=frozenset({"type_spec"}),
             call_types=frozenset({"call_expression"}),
             import_types=frozenset({"import_declaration"}), id_types=_ID_C),
    LangSpec(name="rust", extensions=(".rs",),
             func_types=frozenset({"function_item", "function_signature_item"}),
             class_types=frozenset({"struct_item", "enum_item", "trait_item"}),
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
        seen: dict = {}             # uid -> count

        def uid_for(qual: str) -> str:
            # #ordinal disambiguation for repeated qualnames, in source order — the PythonResolver uses
            # the SAME scheme so duplicate-qualname symbols get matching uids and merge (MR-6).
            u = f"{rel}::{qual}"
            n = seen.get(u, -1) + 1
            seen[u] = n
            return u if n == 0 else f"{u}#{n}"

        def make_node(child, qual: str, kind: str) -> str:
            u = uid_for(qual)
            name = qual.split(".")[-1]
            nodes.append(Node(
                uid=u, type=kind, name=name, body=self._text(child, src)[:2000],
                attrs={"lang": spec.name, "file_uid": rel,
                       "signature": self._signature(child, src),
                       "docstring": self._docstring(child, src, spec),
                       "start_line": child.start_point[0] + 1,
                       "end_line": child.end_point[0] + 1},
            ))
            local.setdefault(name, []).append(u)
            return u

        def walk(node, stack, enclosing, depth=0, in_class=False):
            if depth > _MAX_WALK_DEPTH:     # bound hostile/pathological nesting (security I1)
                return
            for child in node.named_children:
                t = child.type
                # Rust `impl [Trait for] Type { ... }` is a SCOPE on Type (not a class node): it emits
                # INHERITS Type -> Trait and its fns become Type.methods (R6-7/R6-16).
                if spec.name == "rust" and t == "impl_item":
                    impl_type, trait = self._rust_impl(child)
                    if impl_type:
                        if trait and trait != impl_type:   # skip a spurious self-INHERITS (R8-9)
                            refs.append((f"{rel}::{impl_type}", trait, trait, Rel.INHERITS))
                        walk(child, stack + [impl_type], enclosing, depth + 1, in_class=True)
                    else:
                        walk(child, stack, enclosing, depth + 1, in_class)
                    continue
                # Rust `mod a { ... }` is a scope (R8-8) — qualify its items as a.f, like TS namespaces.
                if spec.name == "rust" and t == "mod_item":
                    nm = child.child_by_field_name("name") or self._first_id(child, spec)
                    modname = (nm.text.decode("utf-8", "replace") if hasattr(nm, "text") else nm) if nm else None
                    if modname:
                        walk(child, stack + [modname], enclosing, depth + 1, in_class=False)
                    else:
                        walk(child, stack, enclosing, depth + 1, in_class)
                    continue
                # Go method with a receiver -> Receiver.method, classified as a method (R6-6).
                if spec.name == "go" and t == "method_declaration":
                    recv, mname = self._go_method(child)
                    if mname:
                        qual = ".".join(stack + ([recv, mname] if recv else [mname]))
                        walk(child, stack, make_node(child, qual, "method"), depth + 1)
                    else:
                        walk(child, stack, enclosing, depth + 1)
                    continue
                # JS/TS `const f = (a) => ...` / `const f = function(){}` -> name from the LHS (R6-4);
                # and a class-field arrow `handleClick = (e) => {}` -> a method named from the field (R7-8).
                if spec.name in ("javascript", "typescript") and t in (
                        "variable_declarator", "field_definition", "public_field_definition"):
                    fname = self._arrow_decl(child)
                    if fname:
                        is_field = t != "variable_declarator"
                        kind = "method" if (in_class or is_field) else "function"
                        u = make_node(child, ".".join(stack + [fname]), kind)
                        walk(child, stack + [fname], u, depth + 1, in_class=in_class)
                    else:
                        walk(child, stack, enclosing, depth + 1, in_class)
                    continue
                # TS `namespace A {}` / `module A.B {}` -> a (non-method) scope so members qualify as
                # A.run / A.B.run instead of fusing across namespaces (R7-5).
                if spec.name in ("javascript", "typescript") and t in ("internal_module", "module", "namespace"):
                    nm = child.child_by_field_name("name")
                    if nm is not None and nm.type == "string":     # `declare module "x"` -> strip quotes (R8-7)
                        frag = next((c for c in nm.named_children if c.type == "string_fragment"), None)
                        nsname = frag.text.decode("utf-8", "replace") if frag is not None else \
                            nm.text.decode("utf-8", "replace").strip('"').strip("'")
                    else:
                        nsname = nm.text.decode("utf-8", "replace") if nm is not None else self._first_id(child, spec)
                    if nsname:
                        walk(child, stack + [nsname.split(".")[-1]], enclosing, depth + 1, in_class=False)
                    else:
                        walk(child, stack, enclosing, depth + 1, in_class)
                    continue

                if t in spec.func_types or t in spec.class_types:
                    name = self._name(child, spec)
                    if not name:
                        walk(child, stack, enclosing, depth + 1, in_class)
                        continue
                    is_class = t in spec.class_types
                    # `method` only when the immediate enclosing scope is a class (R6-19).
                    kind = "class" if is_class else ("method" if in_class else "function")
                    u = make_node(child, ".".join(stack + [name]), kind)
                    if is_class:
                        for base in self._base_names(child, src, spec):
                            refs.append((u, base, base, Rel.INHERITS))
                    walk(child, stack + [name], u, depth + 1, in_class=is_class)
                elif t in spec.import_types:
                    imports.update(self._import_names(child, src, spec))
                    walk(child, stack, enclosing, depth + 1, in_class)
                elif t in spec.call_types:
                    name, rootname = self._callee(child, spec, src)
                    if name and enclosing:
                        refs.append((enclosing, name, rootname, Rel.CALLS))
                    walk(child, stack, enclosing, depth + 1, in_class)
                else:
                    walk(child, stack, enclosing, depth + 1, in_class)

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
            if nm.type == "computed_property_name":   # `[expr]() {}` — no stable name, skip (R8-10)
                return None
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
        """Base/super types for an INHERITS edge, per language (R6-5)."""
        out: list = []
        if spec.name == "python":
            supers = node.child_by_field_name("superclasses")
            if supers is not None:
                out = [c.text.decode("utf-8", "replace").split(".")[-1]
                       for c in supers.named_children if c.type in spec.id_types]
        elif spec.name in ("javascript", "typescript"):
            for c in node.named_children:
                if c.type != "class_heritage":
                    continue
                for h in c.named_children:                 # JS: bare ids; TS: extends/implements clauses
                    if h.type in spec.id_types:
                        out.append(h.text.decode("utf-8", "replace").split(".")[-1])
                    elif h.type in ("extends_clause", "implements_clause", "extends_type_clause"):
                        out += [g.text.decode("utf-8", "replace").split(".")[-1]
                                for g in h.named_children if g.type in spec.id_types]
        return out

    @staticmethod
    def _head_type(node):
        """The head type_identifier of a type position, looking through generic_type (`Wrap<T>`),
        scoped_type_identifier (`a::B`) and reference_type (`&T`) so generic impls resolve (R7-2)."""
        if node is None:
            return None
        if node.type == "type_identifier":
            return node.text.decode("utf-8", "replace").split("::")[-1]
        stack = list(node.named_children)
        while stack:                                   # BFS for the first type_identifier descendant
            n = stack.pop(0)
            if n.type == "type_identifier":
                return n.text.decode("utf-8", "replace").split("::")[-1]
            stack.extend(n.named_children)
        return None

    def _rust_impl(self, node):
        """``impl [Trait for] Type`` -> (implementing type, trait or None). Uses the impl node's
        ``type``/``trait`` fields (populated even for generic impls), falling back to the direct type
        positions for older grammars (R6-7, R7-2)."""
        impl_type = self._head_type(node.child_by_field_name("type"))
        trait = self._head_type(node.child_by_field_name("trait"))
        if impl_type is None:
            cands = [c for c in node.named_children
                     if c.type in ("type_identifier", "generic_type", "scoped_type_identifier")]
            if cands:
                impl_type = self._head_type(cands[-1])
                if len(cands) > 1 and trait is None:
                    trait = self._head_type(cands[0])
        return impl_type, trait

    def _go_method(self, node):
        """Go ``func (r Recv) M()`` -> ('Recv', 'M'). Receiver is the first parameter_list (R6-6)."""
        name = node.child_by_field_name("name")
        mname = name.text.decode("utf-8", "replace") if name is not None else None
        if mname is None:
            mname = next((c.text.decode("utf-8", "replace") for c in node.named_children
                          if c.type == "field_identifier"), None)
        receiver = node.child_by_field_name("receiver")
        if receiver is None:
            receiver = next((c for c in node.named_children if c.type == "parameter_list"), None)
        recv = None
        if receiver is not None:
            for pd in receiver.named_children:
                for tc in pd.named_children:
                    if tc.type in ("type_identifier", "pointer_type", "generic_type"):
                        recv = tc.text.decode("utf-8", "replace").lstrip("*&").split("[")[0].split(".")[-1]
                        break
                if recv:
                    break
        return recv, (mname.split(".")[-1] if mname else None)

    @staticmethod
    def _arrow_decl(node):
        """A JS/TS ``variable_declarator`` / class field whose value is an arrow/function -> its name
        (R6-4). A JS class field names via the ``property`` field (property_identifier), TS via ``name``
        (R8-4)."""
        val = node.child_by_field_name("value")
        if val is None or val.type not in ("arrow_function", "function", "function_expression"):
            return None
        nm = node.child_by_field_name("name") or node.child_by_field_name("property")
        if nm is not None and nm.type in ("identifier", "property_identifier"):
            return nm.text.decode("utf-8", "replace").split(".")[-1]
        return next((c.text.decode("utf-8", "replace") for c in node.named_children
                     if c.type in ("identifier", "property_identifier")), None)

    def _import_names(self, node, src: bytes, spec: LangSpec) -> set:
        out: set = set()
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type in spec.id_types:
                txt = n.text.decode("utf-8", "replace")
                out.add(txt)
                out.add(txt.split(".")[-1])
            # Go imports are quoted paths (`import "net/http"`), not identifiers — capture the package
            # name (last path segment) so cross-package calls are import-scoped not bare-global (R6-17).
            elif spec.name == "go" and n.type in ("interpreted_string_literal", "import_spec"):
                seg = self._text(n, src).strip().strip('"').strip("`").rstrip("/").split("/")[-1]
                if seg:
                    out.add(seg)
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
