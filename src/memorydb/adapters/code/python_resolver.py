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
_SELF_METHOD = 0.92  # self.method() / cls.method() resolved against the enclosing class. >0.9 so it
                     # strictly beats the coarse tree-sitter 0.9 in-file edge and keeps its provenance (R8-5)
_STAR = 0.5         # candidate via a single `from m import *`

_MAX_DEPTH = 200    # AST nesting cap — a hostile deeply-nested file must not blow the recursion limit
                    # and abort the index (security MR-1; mirrors the CodeAdapter's _MAX_WALK_DEPTH).


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

    def extract(self, path: str, data: Optional[bytes] = None) -> Extraction:
        rel = os.path.relpath(path, self.repo_root).replace(os.sep, "/")
        try:
            if data is None:                   # reuse the indexer's already-read bytes when given (MR-15)
                with open(path, "rb") as fh:
                    data = fh.read()
            text = data.decode("utf-8", "replace")
            tree = ast.parse(text)
            stab = symtable.symtable(text, rel, "exec")
            return _Extractor(rel, text, tree, stab).run()
        except Exception:
            # Broad on purpose (MR-1): a pathological file (deep AST -> RecursionError, which is a
            # RuntimeError not in the old narrow tuple) must yield an empty Extraction, not abort.
            return Extraction()


class _Extractor:
    def __init__(self, rel: str, text: str, tree: ast.AST, stab) -> None:
        self.rel = rel
        self.text = text
        self.tree = tree
        self.pkg = self._package_parts(rel)            # dotted package of this module, as path parts
        self.imports: dict = {}                        # alias -> ("sym", relpath, name)  for `from m import name`
        self.mod_imports: dict = {}                    # alias -> relpath  for module-attribute access `mod.f()`
        self.star: list = []                           # relpaths of `from m import *`
        self.module_defs: dict = {}                    # top-level name -> effective uid
        self.class_methods: dict = {}                  # class qualname -> {method name -> effective uid}
        self.nodes: list = []
        self.edges: list = []
        self._seen: dict = {}                          # uid -> count, for #ordinal disambiguation (MR-6)
        self._def_uid: dict = {}                       # id(ast def node) -> its ordinal uid (R6-1)
        self._def_stub: dict = {}                      # module def name -> is it an @overload-style stub
        self._cm_stub: dict = {}                       # (classq, method name) -> stub
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
            if node.level > len(self.pkg):             # escapes the top package (an ImportError) — don't
                return None                            # guess, else it collides onto a top-level module (MR-13)
            base = self.pkg[: len(self.pkg) - (node.level - 1)]
            parts = list(base) + (node.module.split(".") if node.module else [])
        else:
            parts = node.module.split(".") if node.module else []
        return "/".join(parts) + ".py" if parts else None

    def _collect_imports(self) -> None:
        for n in ast.walk(self.tree):                  # include nested (function-local) imports
            if isinstance(n, ast.Import):
                for a in n.names:
                    if a.asname:                       # import x.y as z  ->  z = x/y.py
                        self.mod_imports[a.asname] = _module_relpath(a.name)
                    else:                              # import x.y.z binds the TOP-LEVEL `x` -> x.py (MR-10)
                        top = a.name.split(".")[0]
                        self.mod_imports.setdefault(top, _module_relpath(top))
            elif isinstance(n, ast.ImportFrom):
                target = self._from_relpath(n)
                if any(al.name == "*" for al in n.names):
                    if target:
                        self.star.append(target)
                    continue
                if not target:
                    continue
                pkgdir = target[:-3]                    # <pkg>.py -> <pkg>/ for submodule resolution
                for a in n.names:
                    local = a.asname or a.name
                    # `from m import name`: `name` may be a re-exported SYMBOL (bare call name()) or a
                    # SUBMODULE (attribute call name.f()). Register both interpretations (MR-11).
                    self.imports[local] = ("sym", target, a.name)
                    self.mod_imports.setdefault(local, f"{pkgdir}/{a.name}.py")

    # --- defs / nodes ------------------------------------------------------
    def _uid(self, qual: str) -> str:
        """relpath::qual with the same #ordinal disambiguation the CodeAdapter uses, so duplicate
        qualnames (e.g. @overload stubs + impl) get matching uids in both adapters and merge (MR-6)."""
        u = f"{self.rel}::{qual}"
        n = self._seen.get(u, -1) + 1
        self._seen[u] = n
        return u if n == 0 else f"{u}#{n}"

    @staticmethod
    def _is_stub(child: ast.AST) -> bool:
        """A def whose body is only `...` / `pass` / a docstring — i.e. a typing.overload stub."""
        for s in getattr(child, "body", []):
            if isinstance(s, ast.Pass):
                continue
            if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant):
                continue
            return False
        return True

    def _collect_defs(self, node: ast.AST, stack: list, depth: int = 0,
                      parent_class: Optional[str] = None) -> None:
        if depth > _MAX_DEPTH:
            return
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = ".".join(stack + [child.name])
                uid = self._uid(qual)
                self._def_uid[id(child)] = uid          # _walk_edges reuses this exact ordinal uid (R6-1)
                is_class = isinstance(child, ast.ClassDef)
                # `method` only when the IMMEDIATE enclosing scope is a class; a function nested in a
                # function is a `function`, not a `method` (R6-19).
                kind = "class" if is_class else ("method" if parent_class else "function")
                stub = self._is_stub(child)
                doc = ast.get_docstring(child) or ""
                self.nodes.append(Node(
                    uid=uid, type=kind, name=child.name,
                    body=(ast.get_source_segment(self.text, child) or "")[:2000],
                    attrs={"lang": "python", "file_uid": self.rel,
                           # cap signature/docstring like body[:2000] — source is attacker-controlled,
                           # an unbounded one-liner shouldn't blow up storage or the context payload (PR3-6)
                           "signature": self._signature(child)[:512],
                           "docstring": (doc.splitlines()[0] if doc else "")[:512],
                           "start_line": child.lineno,
                           "end_line": getattr(child, "end_lineno", child.lineno)},
                ))
                if not stack:                           # module-level def: prefer the real impl over a stub
                    if child.name not in self.module_defs or (self._def_stub.get(child.name) and not stub):
                        self.module_defs[child.name] = uid
                        self._def_stub[child.name] = stub
                if parent_class is not None:            # method: name -> effective (non-stub) uid (R6-12)
                    cm = self.class_methods.setdefault(parent_class, {})
                    key = (parent_class, child.name)
                    if child.name not in cm or (self._cm_stub.get(key) and not stub):
                        cm[child.name] = uid
                        self._cm_stub[key] = stub
                self._collect_defs(child, stack + [child.name], depth + 1,
                                   parent_class=qual if is_class else None)
            else:
                # Descend into control-flow blocks (if/for/try/with) too, so a conditionally-defined
                # def/class is collected as a node — and with the SAME uid _walk_edges will compute,
                # since both now traverse every node (R6-1).
                self._collect_defs(child, stack, depth + 1, parent_class)

    def _signature(self, node: ast.AST) -> str:
        lines = self.text.splitlines()
        i = node.lineno - 1
        return lines[i].strip() if 0 <= i < len(lines) else ""

    # --- edges -------------------------------------------------------------
    def _walk_edges(self, node: ast.AST, stack: list, classq: Optional[str],
                    enclosing_uid: Optional[str], depth: int = 0) -> None:
        if depth > _MAX_DEPTH:    # recurses into every node, incl. deep attr/call chains (MR-1)
            return
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = ".".join(stack + [child.name])
                uid = self._def_uid.get(id(child)) or f"{self.rel}::{qual}"  # same ordinal uid (R6-1)
                if isinstance(child, ast.ClassDef):
                    for base in child.bases:
                        r = self._resolve(base, stack, classq)
                        if r:
                            self.edges.append(Edge(src=uid, dst=r[0], relation=Rel.INHERITS,
                                                   confidence=r[1], source="python-ast"))
                    self._walk_edges(child, stack + [child.name], qual, uid, depth + 1)
                else:
                    self._walk_edges(child, stack + [child.name], classq, uid, depth + 1)
            elif isinstance(child, ast.Call):
                if enclosing_uid:
                    r = self._resolve(child.func, stack, classq)
                    if r:
                        self.edges.append(Edge(src=enclosing_uid, dst=r[0], relation=Rel.CALLS,
                                               confidence=r[1], source="python-ast"))
                self._walk_edges(child, stack, classq, enclosing_uid, depth + 1)
            else:
                self._walk_edges(child, stack, classq, enclosing_uid, depth + 1)

    def _resolve(self, fn: ast.AST, stack: list, classq: Optional[str]):
        """Resolve a callee/base expression to ``(target_uid, confidence)`` or ``None`` (skip)."""
        if isinstance(fn, ast.Name):
            name = fn.id
            scope = ".".join(stack)
            if name in self.locals_by_scope.get(scope, ()):  # shadowed by a local/param -> not a def
                return None
            if name in self.module_defs:
                return (self.module_defs[name], _LOCAL)
            sym = self.imports.get(name)
            if sym:
                return (f"{sym[1]}::{sym[2]}", _IMPORT_SYM)
            if len(self.star) == 1:                          # single star import: a plausible candidate
                return (f"{self.star[0]}::{name}", _STAR)
            return None
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            recv, attr = fn.value.id, fn.attr
            modrel = self.mod_imports.get(recv)
            if modrel:
                return (f"{modrel}::{attr}", _IMPORT_ATTR)
            methods = self.class_methods.get(classq) if classq else None
            if recv in ("self", "cls") and methods and attr in methods:
                return (methods[attr], _SELF_METHOD)        # effective (non-stub) ordinal uid (R6-12)
        return None

    # --- symtable scope ----------------------------------------------------
    def _scope_locals(self, stab) -> dict:
        """qualname(function) -> {names bound as a parameter or a plain local variable}. Used to avoid
        resolving a call whose name is actually a local (shadowing a module-level def of the same name)."""
        out: dict = {}

        def walk(table, stack, depth=0):
            if depth > _MAX_DEPTH:
                return
            if table.get_type() == "function":
                locs = set()
                for sym in table.get_symbols():
                    try:
                        # Include namespace symbols (nested def/class names) so a call to a name a
                        # nested def shadows is skipped, not wrongly bound to a module def (MR-8). The
                        # is_imported exclusion keeps genuine imports resolvable.
                        if sym.is_parameter() or (sym.is_local() and not sym.is_imported()):
                            locs.add(sym.get_name())
                    except Exception:
                        continue
                # Union same-named sibling scopes (e.g. @property + @x.setter) instead of overwriting,
                # so the shadowing set is never silently lost — skip if shadowed in ANY sibling (MR-9).
                out.setdefault(".".join(stack), set()).update(locs)
            for ch in table.get_children():
                walk(ch, stack + [ch.get_name()], depth + 1)

        for ch in stab.get_children():
            walk(ch, [ch.get_name()])
        return out
