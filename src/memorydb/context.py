"""Context builder & token-budgeted packing (context-builder-packing spec; TD-007/006).

Turns a retrieval result (``{seeds, nodes, edges, depths}`` for EXPLAIN, ``{references}`` for LOCATE)
into **LLM-ready context within a token budget** — packing *relationships*, not a bag of chunks. The
payoff over classic RAG: the model sees structure with ``file:line`` provenance.

Deterministic and tokenizer-agnostic: a ``TokenCounter`` port (default ``HeuristicCounter`` ≈ chars/4)
lets a caller inject the model's real tokenizer. Loss is signalled at two levels, never silently:
*budget*-level loss (whole nodes/refs that did not fit, or a card byte-cut to fit the budget) is
reported via ``dropped``/``truncated``; *field*-level display clipping (a single signature/docstring/
qualname capped at ``_FIELD_CAP`` regardless of budget — a shaping/anti-spoofing bound, not a budget
effect) is signalled in-band by a literal ``…`` and is intentionally distinct from ``truncated``.

Source-derived text (signatures, docstrings, symbol names) comes from an *indexed repo*, which is
attacker-controlled — it is sanitized before interpolation so it cannot spoof markdown structure
(fake headers, fences, phantom Relationships) in the LLM-consumed context.
"""
from __future__ import annotations

import re
from typing import Optional, Protocol

from pydantic import BaseModel, Field

# Ranking weights (TD-007) and packing reserves.
_W_SCORE, _W_DEPTH, _W_CONF = 0.5, 0.3, 0.2
_RESERVE = 0.15   # fraction of the budget held back for the Relationships block
_SAFETY = 0.9     # pack to budget*0.9 — the chars/4 heuristic under-counts punctuation-dense code (C)
_FIELD_CAP = 240  # max rendered chars for a source-derived field (bounds the structure-spoof payload)


class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


class HeuristicCounter:
    """Zero-dep ≈ chars/4 token estimate. Inject a real tokenizer when exactness matters."""

    def count(self, text: str) -> int:
        return max(1, (len(text) + 3) // 4)


class ContextResult(BaseModel):
    """A token-budgeted, LLM-ready packing of a retrieval result. ``truncated``/``dropped`` make any
    *budget*-level loss explicit (``dropped`` = nodes/refs that did not fit; ``truncated`` = either
    some were dropped *or* a single oversized card was byte-cut to fit the budget). Per-field display
    clipping (``…``, bounded by ``_FIELD_CAP`` independent of budget) is a separate, in-band signal and
    deliberately does *not* set ``truncated``. ``used_tokens`` never exceeds ``budget_tokens`` (which is
    itself clamped to ``>= 0``)."""

    text: str = ""
    cards: list = Field(default_factory=list)   # structured form, for non-markdown consumers
    uids: list = Field(default_factory=list)    # uids that made it into the context, in order
    used_tokens: int = 0
    budget_tokens: int = 0
    dropped: int = 0                            # nodes/refs that did not fit (reported, not hidden)
    truncated: bool = False
    intent: str = ""


def _qual(uid: str) -> str:
    return uid.split("::", 1)[1] if "::" in uid else uid


def _clip(text, cap: int = _FIELD_CAP) -> str:
    """Collapse newlines to spaces and cap length — bounds size and prevents a multi-line source
    field from injecting extra logical rows. No markdown escaping (use ``_safe`` for that)."""
    t = " ".join(str(text or "").splitlines()).strip()
    return (t[:cap] + "…") if len(t) > cap else t


# Single chars that start a markdown block on their own: ATX header `#`, blockquote `>`, table `|`,
# the list/thematic-break markers `-*+`, and setext-H1 underline `=` (a leading `=` is never a valid
# identifier start, so escaping it is safe — re-review-2 RR2-1). A single leading `~`/`_` is NOT
# structure (the very common Python `_private`/`__init__`); those matter only as a 3+ run.
_LEAD1 = "#>|-*+="
_LEAD_RUNS = ("~~~", "___", "===", "---", "***", "+++")
_RULE_CHARS = set("-_*=+ ")                 # a line of ONLY these (>=3 markers) is a (possibly spaced) rule
_LINKREF_RE = re.compile(r"\[[^\]]*\]:")    # `[label]: …` link-reference definition


def _safe(text, cap: int = _FIELD_CAP) -> str:
    """Markdown-neutralize source-derived text before interpolation: clip (newline-collapse + cap),
    strip backticks (cannot open/close code fences or the signature backticks), and escape a leading
    structural marker so it cannot masquerade as a header/quote/list/rule/fence/HTML/link-ref/table.
    LLM-only sink, but an indexed repo is attacker-controlled (PR3-3/PR3-6/PR3-7; ~~~/___ fences +
    empty-string guard from re-review C4/C7/C9; setext `=`, spaced rules, link-ref and leading `<` from
    re-review-2 RR2-1/RR2-2). Run-aware: a single leading `_`/`~` is left alone (snake_case/dunder).
    Ordered lists (`1.`) are deliberately NOT escaped: a renumbered list is benign and escaping it would
    mangle the very common numbered-docstring case (accepted trade-off, LLM-only sink)."""
    t = _clip(text, cap).replace("`", "")
    if not t:                                            # '' is a substring of every string — an empty
        return t                                         # field must stay empty, not become `\` (C7/C9)
    if (t[:1] in _LEAD1 or t[:3] in _LEAD_RUNS or t[:1] == "<"     # block starter / fence-run / HTML
            or _LINKREF_RE.match(t)                                # link-reference definition
            or (set(t) <= _RULE_CHARS and sum(c != " " for c in t) >= 3)):   # spaced thematic rule `_ _ _`
        t = "\\" + t
    return t


def _loc(node: dict) -> str:
    attrs = node.get("attrs") or {}
    uid = node["uid"]
    path = attrs.get("file_uid") or (uid.split("::", 1)[0] if "::" in uid else uid)
    line = attrs.get("start_line")
    return f"{path}:{line}" if line else path


class ContextBuilder:
    def __init__(self, counter: Optional[TokenCounter] = None, max_cards: int = 100) -> None:
        self.counter = counter or HeuristicCounter()
        self.max_cards = max_cards

    def build(self, result: dict, budget_tokens: int, fmt: str = "markdown") -> ContextResult:
        budget = max(0, budget_tokens)   # clamp BOTH routes — negative budgets are degenerate (PR3-1)
        intent = result.get("intent", "")
        if intent == "LOCATE":
            return self._build_locate(result, budget)
        return self._build_explain(result, budget, intent or "EXPLAIN")

    # --- EXPLAIN ----------------------------------------------------------
    def _build_explain(self, result: dict, budget: int, intent: str) -> ContextResult:
        nodes = list(result.get("nodes", []))
        if not nodes:
            return ContextResult(budget_tokens=budget, intent=intent)
        seeds = list(result.get("seeds", []))
        depths = result.get("depths", {}) or {}
        seed_rank = {sid: 1.0 - i / len(seeds) for i, sid in enumerate(seeds)}   # vector-rank proxy
        conf = self._edge_confidence(result.get("edges", []), nodes)

        def rank(n):
            d = depths.get(n["id"], depths.get(str(n["id"]), 99))
            return (_W_SCORE * seed_rank.get(n["id"], 0.0)
                    + _W_DEPTH * (1.0 / (1.0 + d))
                    + _W_CONF * conf.get(n["uid"], 0.0))

        ordered = sorted(nodes, key=lambda n: (-rank(n), n["uid"]))[: self.max_cards]
        dropped = len(nodes) - len(ordered)

        effective = int(budget * _SAFETY)
        reserve = int(effective * _RESERVE)
        card_budget = effective - reserve

        rels_by = self._rel_index(result.get("edges", []))
        cards_md, card_dicts, uids, used, card_truncated = [], [], [], 0, False
        for n in ordered:
            calls, called_by = rels_by.get(n["uid"], ([], []))
            card = self._card_md(n, calls, called_by)
            cost = self.counter.count(card)
            if used == 0 and cost > card_budget:        # first card alone overflows the card budget
                if card_budget <= 0:                    # ...and nothing fits at all -> drop everything
                    dropped += len(ordered)
                    break
                card = card[: card_budget * 4]          # truncate the single oversized card *in*
                if card.count("`") % 2:                  # the cut sliced through a `signature` wrapper —
                    i = card.rfind("`")                  # drop the dangling unbalanced backtick so it
                    card = card[:i] + card[i + 1:]       # can't open a stray code span (re-review-2 RR2-3)
                cards_md.append(card)
                card_dicts.append(self._card_dict(n, calls, called_by))
                uids.append(n["uid"])
                used += self.counter.count(card)
                card_truncated = True                   # report the byte-cut (PR3-2), not a clean fit
                dropped += len(ordered) - 1
                break
            if used + cost > card_budget:
                dropped += 1
                continue
            cards_md.append(card)
            card_dicts.append(self._card_dict(n, calls, called_by))
            uids.append(n["uid"])
            used += cost

        # Relationships block among the INCLUDED nodes, highest-confidence first, within the reserve.
        rel_lines, used = self._relationships(result.get("edges", []), set(uids), reserve, used,
                                              budget - used)
        sections = list(cards_md)
        if rel_lines:
            sections.append("**Relationships**\n" + "\n".join(rel_lines))
        text = "\n\n".join(sections)
        return ContextResult(text=text, cards=card_dicts, uids=uids,
                             used_tokens=used, budget_tokens=budget, dropped=dropped,
                             truncated=dropped > 0 or card_truncated, intent=intent)

    def _card_md(self, node: dict, calls: list, called_by: list) -> str:
        attrs = node.get("attrs") or {}
        # _loc()/file_uid and the uid prefix come from the (attacker-controlled) repo path too — a
        # newline in a filename would forge a new header/fence/section, so sanitize it like the rest
        # (re-review C2). type is ours (function/class/…) but _clip bounds it defensively.
        parts = [f"### {_safe(node.get('name', ''), 120)}  ·  {_clip(node.get('type', ''), 40)}"
                 f"  ·  {_safe(_loc(node), 200)}"]
        sig = _safe(attrs.get("signature") or "")
        if sig:
            parts.append(f"`{sig}`")
        doc = _safe(attrs.get("docstring") or "")
        if doc:
            parts.append(doc)
        rel = []
        # clip-then-set-then-sort, matching _card_dict exactly so markdown and structured forms dedupe
        # identically (re-review C6).
        if calls:
            rel.append("→ calls: " + ", ".join(sorted(set(_clip(c, 80) for c in calls))))
        if called_by:
            rel.append("← called by: " + ", ".join(sorted(set(_clip(c, 80) for c in called_by))))
        if rel:
            parts.append("   ".join(rel))
        return "\n".join(parts)

    @staticmethod
    def _card_dict(node: dict, calls: list, called_by: list) -> dict:
        """Structured form for non-markdown consumers (PR3-4) — the same fields the card renders,
        unparsed. Source-derived strings are length-clipped (not markdown-escaped: a JSON consumer
        wants the raw-ish value, just bounded)."""
        attrs = node.get("attrs") or {}
        uid = node["uid"]
        return {
            "uid": uid,
            "name": _clip(node.get("name") or "", 120),
            "type": node.get("type"),
            "file": _clip(attrs.get("file_uid") or (uid.split("::", 1)[0] if "::" in uid else uid), 200),
            "line": attrs.get("start_line"),
            "signature": _clip(attrs.get("signature") or "", 512),
            "docstring": _clip(attrs.get("docstring") or "", 512),
            "calls": sorted(set(_clip(c, 80) for c in calls)),
            "called_by": sorted(set(_clip(c, 80) for c in called_by)),
        }

    @staticmethod
    def _edge_confidence(edges: list, nodes: list) -> dict:
        present = {n["uid"] for n in nodes}
        out: dict = {}
        for e in edges:
            for u in (e["src"], e["dst"]):
                if u in present:
                    out[u] = max(out.get(u, 0.0), e.get("confidence", 0.0))
        return out

    @staticmethod
    def _rel_index(edges: list) -> dict:
        """uid -> (calls[names], called_by[names]) for the CALLS edges."""
        out: dict = {}
        for e in edges:
            if e.get("relation") != "CALLS":
                continue
            out.setdefault(e["src"], ([], []))[0].append(_qual(e["dst"]))
            out.setdefault(e["dst"], ([], []))[1].append(_qual(e["src"]))
        return out

    def _relationships(self, edges: list, included: set, reserve: int, used: int, hard_remaining: int):
        cap = min(reserve, max(0, hard_remaining))
        if cap <= 0:                              # no room for a single line -> skip the O(E log E) sort
            return [], used
        # Only edges with BOTH endpoints included can be emitted — filter before sorting (PR3-5).
        incident = [e for e in edges if e["src"] in included and e["dst"] in included]
        lines, spent = [], 0
        # `relation` completes the total order: two edges between the same pair at equal confidence
        # (e.g. A INHERITS B + A CALLS B) would otherwise render in plan-dependent order (RR3-1; same
        # class as MR-17, which fixed the node path but missed this edge path).
        for e in sorted(incident, key=lambda x: (-x.get("confidence", 0.0), x["src"], x["dst"], x.get("relation", ""))):
            line = f"{_clip(_qual(e['src']), 80)} --{e['relation']}--> {_clip(_qual(e['dst']), 80)}"
            c = self.counter.count(line)
            if spent + c > cap:
                break
            lines.append(line); spent += c
        return lines, used + spent

    # --- LOCATE -----------------------------------------------------------
    def _build_locate(self, result: dict, budget: int) -> ContextResult:
        sym = _safe(result.get("symbol") or "?", 80)
        refs = result.get("references", [])
        ceiling = int(budget * _SAFETY)
        header = f"**{sym}** — used at:"
        hcost = self.counter.count(header)
        if hcost > ceiling:                       # header alone overflows the budget -> nothing fits
            plain = sym[: max(0, ceiling * 4)]    # plain symbol, NO ** markup, to avoid an unbalanced
            return ContextResult(text=plain, uids=[],    # mid-token fragment like '**authen' (C8)
                                 used_tokens=(self.counter.count(plain) if plain else 0),
                                 budget_tokens=budget, dropped=len(refs), truncated=True,
                                 intent="LOCATE")
        used, lines, cards, uids, dropped = hcost, [header], [], [], 0
        for r in refs:
            name, relation = _safe(r.get("src_name") or "", 80), _safe(r.get("relation") or "", 40)
            raw_file = str(r.get("src_uid") or "").split("::", 1)[0]
            # the file part of src_uid is also repo-path-derived — a newline would forge an extra
            # reference row, so sanitize it like name/relation (re-review C3; PR3-7 was incomplete).
            line = f"- {name}  {relation}  (conf {r.get('confidence', 0):.2f})  {_safe(raw_file, 120)}"
            c = self.counter.count(line)
            if used + c > ceiling:
                dropped += 1
                continue
            lines.append(line)
            cards.append({"uid": r.get("src_uid"), "name": _clip(r.get("src_name") or "", 80),
                          "relation": r.get("relation"), "file": _clip(raw_file, 120)})
            uids.append(r.get("src_uid"))
            used += c
        if not refs:
            nr = "(no references)"
            if used + self.counter.count(nr) <= ceiling:
                lines.append(nr); used += self.counter.count(nr)
        return ContextResult(text="\n".join(lines), cards=cards, uids=uids,
                             used_tokens=used, budget_tokens=budget, dropped=dropped,
                             truncated=dropped > 0, intent="LOCATE")
