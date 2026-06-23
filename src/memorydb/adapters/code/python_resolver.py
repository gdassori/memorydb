"""Python precise resolver — high-confidence edges via stdlib ``ast`` + ``symtable``
(python-precise-resolver spec; TD-005).

A Python-specific ``Extractor`` that emits high-confidence (0.9–1.0) CALLS/INHERITS edges, superseding
the coarse name-based edges from the tree-sitter [CodeAdapter](__init__.py) via the store's
MAX-confidence upsert. Pure standard library — no tree-sitter, so Python files resolve even without the
``[code]`` extra. Shares the ``relpath::qualname`` uid scheme so its nodes/edges merge with the coarse
ones.

Resolution is best-effort and **safe by construction**: every edge targets a *computed* uid, and the
indexer only materialises it if both endpoints exist — so an imperfect module-path guess yields a
*skipped* edge, never a wrong one. ``symtable`` is used to skip calls to a name shadowed by a local
variable / parameter (which would otherwise produce a false high-confidence edge to a module-level def
of the same name).
"""
from __future__ import annotations

import ast
import os
import symtable
from typing import Optional

from memorydb.models import Edge, Node, Rel

from . import Extraction

# Confidence tiers (TD-005): precise edges supersede the coarse tree-sitter ones (<=0.9) via MAX upsert.
_LOCAL = 1.0        # bare name bound to a def in this module
_IMPORT_SYM = 0.97  # bare name imported as a symbol: `from m import f` -> f()
_IMPORT_ATTR = 0.95 # attribute on an imported module alias: `import m` -> m.f()
_SELF_METHOD = 0.9  # self.method() / cls.method() resolved against the enclosing class
_STAR = 0.5         # candidate via a single `from m import *`


def _module_relpath(dotted: str) -> str:
    return dotted.replace(".", "/") + ".py"


class PythonResolver:
    """Extractor port (TD-002): ``extract(path) -> Extraction`` for ``.py`` files. Stateless across
    files; cross-module targets are computed by uid and validated by the indexer (existence check)."""

    def __init__(self, repo_root: str = ".") -> None:
        self.repo_root = repo_root

    def handles(self, path: str) -> bool:
        return path.endswith(".py")

    def lang_of(self, path: str) -> Optional[str]:
        return "python" if path.endswith(".py") else None

    def extract(self, path: str) -> Extraction:
        rel = os.path.relpath(path, self.repo_root).replace(os.sep, "/")
        try:
            with open(path, "rb") as fh:
                text = fh.read().decode("utf-8", "replace")
            tree = ast.parse(text)
            stab = symtable.symtable(text, rel, "exec")
        except (SyntaxError, ValueError, OSError):
            return Extraction()  # never fail the index on a bad file
        return _Extractor(rel, text, tree, stab).run()


class _Extractor:
    def __init__(self, rel: str, text: str, tree: ast.AST, stab) -> None:
        self.rel = rel
        self.text = text
        self.tree = tree
        self.pkg = self._package_parts(rel)            # dotted package of this module, as path parts
        self.imports: dict = {}                        # alias -> ("sym", relpath, name) | ("mod", relpath, None)
        self.star: list = []                           # relpaths of `from m import *`
        self.module_defs: dict = {}                    # top-level name -> uid
        self.class_methods: dict = {}                  # class qualname -> {method name}
        self.nodes: list = []
        self.edges: list = []
        self.locals_by_scope = self._scope_locals(stab)

    # --- public ------------------------------------------------------------
    def run(self) -> Extraction:
        self._collect_imports()
        self._collect_defs(self.tree, [])
        self._walk_edges(self.tree, [], None, None)
        return Extraction(nodes=self.nodes, edges=self.edges, pending=[])

    # --- imports -----------------------------------------------------------
    @staticmethod
    def _package_parts(rel: str) -> list:
        parts = rel[:-3].split("/") if rel.endswith(".py") else rel.split("/")
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return parts[:-1]                              # the package directory containing this module

    def _from_relpath(self, node: ast.ImportFrom) -> Optional[str]:
        if node.level:                                 # relative import: resolve against this package
            base = self.pkg[: len(self.pkg) - (node.level - 1)] if (node.level - 1) <= len(self.pkg) else []
            parts = list(base) + (node.module.split(".") if node.module else [])
        else:
            parts = node.module.split(".") if node.module else []
        return "/".join(parts) + ".py" if parts else None

    def _collect_imports(self) -> None:
        for n in ast.walk(self.tree):                  # include nested (function-local) imports
            if isinstance(n, ast.Import):
                for a in n.names:
                    alias = a.asname or a.name.split(".")[0]
                    self.imports[alias] = ("mod", _module_relpath(a.name), None)
            elif isinstance(n, ast.ImportFrom):
                target = self._from_relpath(n)
                if any(al.name == "*" for al in n.names):
                    if target:
                        self.star.append(target)
                    continue
                for a in n.names:
                    if target:
                        self.imports[a.asname or a.name] = ("sym", target, a.name)

    # --- defs / nodes ------------------------------------------------------
    def _collect_defs(self, node: ast.AST, stack: list) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = ".".join(stack + [child.name])
                uid = f"{self.rel}::{qual}"
                is_class = isinstance(child, ast.ClassDef)
                kind = "class" if is_class else ("method" if stack else "function")
                doc = ast.get_docstring(child) or ""
                self.nodes.append(Node(
                    uid=uid, type=kind, name=child.name,
                    body=(ast.get_source_segment(self.text, child) or "")[:2000],
                    attrs={"lang": "python", "file_uid": self.rel,
                           "signature": self._signature(child),
                           "docstring": doc.splitlines()[0] if doc else "",
                           "start_line": child.lineno,
                           "end_line": getattr(child, "end_lineno", child.lineno)},
                ))
                if not stack:
                    self.module_defs.setdefault(child.name, uid)
                if is_class:
                    self.class_methods[qual] = {
                        m.name for m in child.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                    }
                self._collect_defs(child, stack + [child.name])

    def _signature(self, node: ast.AST) -> str:
        lines = self.text.splitlines()
        i = node.lineno - 1
        return lines[i].strip() if 0 <= i < len(lines) else ""

    # --- edges -------------------------------------------------------------
    def _walk_edges(self, node: ast.AST, stack: list, classq: Optional[str], enclosing_uid: Optional[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = ".".join(stack + [child.name])
                uid = f"{self.rel}::{qual}"
                if isinstance(child, ast.ClassDef):
                    for base in child.bases:
                        r = self._resolve(base, stack, classq)
                        if r:
                            self.edges.append(Edge(src=uid, dst=r[0], relation=Rel.INHERITS,
                                                   confidence=r[1], source="python-ast"))
                    self._walk_edges(child, stack + [child.name], qual, uid)
                else:
                    self._walk_edges(child, stack + [child.name], classq, uid)
            elif isinstance(child, ast.Call):
                if enclosing_uid:
                    r = self._resolve(child.func, stack, classq)
                    if r:
                        self.edges.append(Edge(src=enclosing_uid, dst=r[0], relation=Rel.CALLS,
                                               confidence=r[1], source="python-ast"))
                self._walk_edges(child, stack, classq, enclosing_uid)
            else:
                self._walk_edges(child, stack, classq, enclosing_uid)

    def _resolve(self, fn: ast.AST, stack: list, classq: Optional[str]):
        """Resolve a callee/base expression to ``(target_uid, confidence)`` or ``None`` (skip)."""
        if isinstance(fn, ast.Name):
            name = fn.id
            scope = ".".join(stack)
            if name in self.locals_by_scope.get(scope, ()):  # shadowed by a local/param -> not a def
                return None
            if name in self.module_defs:
                return (self.module_defs[name], _LOCAL)
            kind = self.imports.get(name)
            if kind and kind[0] == "sym":
                return (f"{kind[1]}::{kind[2]}", _IMPORT_SYM)
            if len(self.star) == 1:                          # single star import: a plausible candidate
                return (f"{self.star[0]}::{name}", _STAR)
            return None
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            recv, attr = fn.value.id, fn.attr
            kind = self.imports.get(recv)
            if kind and kind[0] == "mod":
                return (f"{kind[1]}::{attr}", _IMPORT_ATTR)
            if recv in ("self", "cls") and classq and attr in self.class_methods.get(classq, ()):
                return (f"{self.rel}::{classq}.{attr}", _SELF_METHOD)
        return None

    # --- symtable scope ----------------------------------------------------
    def _scope_locals(self, stab) -> dict:
        """qualname(function) -> {names bound as a parameter or a plain local variable}. Used to avoid
        resolving a call whose name is actually a local (shadowing a module-level def of the same name)."""
        out: dict = {}

        def walk(table, stack):
            if table.get_type() == "function":
                locs = set()
                for sym in table.get_symbols():
                    try:
                        if sym.is_parameter() or (sym.is_local() and not sym.is_namespace()
                                                  and not sym.is_imported()):
                            locs.add(sym.get_name())
                    except Exception:
                        continue
                out[".".join(stack)] = locs
            for ch in table.get_children():
                walk(ch, stack + [ch.get_name()])

        for ch in stab.get_children():
            walk(ch, [ch.get_name()])
        return out
