"""Context builder & token-budgeted packing (context-builder-packing spec; TD-007/006).

Turns a retrieval result (``{seeds, nodes, edges, depths}`` for EXPLAIN, ``{references}`` for LOCATE)
into **LLM-ready context within a token budget** — packing *relationships*, not a bag of chunks. The
payoff over classic RAG: the model sees structure with ``file:line`` provenance.

Deterministic and tokenizer-agnostic: a ``TokenCounter`` port (default ``HeuristicCounter`` ≈ chars/4)
lets a caller inject the model's real tokenizer. Dropped content is *reported* (``dropped``), never
silently truncated.
"""
from __future__ import annotations

from typing import Optional, Protocol

from pydantic import BaseModel, Field

# Ranking weights (TD-007) and packing reserves.
_W_SCORE, _W_DEPTH, _W_CONF = 0.5, 0.3, 0.2
_RESERVE = 0.15   # fraction of the budget held back for the Relationships block
_SAFETY = 0.9     # pack to budget*0.9 — the chars/4 heuristic under-counts punctuation-dense code (C)


class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


class HeuristicCounter:
    """Zero-dep ≈ chars/4 token estimate. Inject a real tokenizer when exactness matters."""

    def count(self, text: str) -> int:
        return max(1, (len(text) + 3) // 4)


class ContextResult(BaseModel):
    """A token-budgeted, LLM-ready packing of a retrieval result. ``truncated``/``dropped`` make any
    budget overflow explicit; ``used_tokens`` never exceeds ``budget_tokens``."""

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
        intent = result.get("intent", "")
        if intent == "LOCATE":
            return self._build_locate(result, budget_tokens)
        return self._build_explain(result, max(0, budget_tokens), intent or "EXPLAIN")

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
        cards, uids, used = [], [], 0
        for n in ordered:
            calls, called_by = rels_by.get(n["uid"], ([], []))
            card = self._card_md(n, calls, called_by)
            cost = self.counter.count(card)
            if used == 0 and cost > card_budget:        # first card alone overflows -> truncate it in
                card = card[: max(1, card_budget * 4)]
                cards.append(card); uids.append(n["uid"]); used += self.counter.count(card)
                dropped += len(ordered) - 1
                break
            if used + cost > card_budget:
                dropped += 1
                continue
            cards.append(card); uids.append(n["uid"]); used += cost

        # Relationships block among the INCLUDED nodes, highest-confidence first, within the reserve.
        rel_lines, used = self._relationships(result.get("edges", []), set(uids), reserve, used,
                                              budget - used)
        sections = list(cards)
        if rel_lines:
            sections.append("**Relationships**\n" + "\n".join(rel_lines))
        text = "\n\n".join(sections)
        return ContextResult(text=text, cards=[{"uid": u} for u in uids], uids=uids,
                             used_tokens=used, budget_tokens=budget, dropped=dropped,
                             truncated=dropped > 0, intent=intent)

    def _card_md(self, node: dict, calls: list, called_by: list) -> str:
        attrs = node.get("attrs") or {}
        parts = [f"### {node['name']}  ·  {node['type']}  ·  {_loc(node)}"]
        sig = (attrs.get("signature") or "").strip()
        if sig:
            parts.append(f"`{sig}`")
        doc = (attrs.get("docstring") or "").strip()
        if doc:
            parts.append(doc)
        rel = []
        if calls:
            rel.append("→ calls: " + ", ".join(sorted(set(calls))))
        if called_by:
            rel.append("← called by: " + ", ".join(sorted(set(called_by))))
        if rel:
            parts.append("   ".join(rel))
        return "\n".join(parts)

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
        lines: list = []
        spent = 0
        cap = min(reserve, max(0, hard_remaining))
        for e in sorted(edges, key=lambda x: (-x.get("confidence", 0.0), x["src"], x["dst"])):
            if e["src"] not in included or e["dst"] not in included:
                continue
            line = f"{_qual(e['src'])} --{e['relation']}--> {_qual(e['dst'])}"
            c = self.counter.count(line)
            if spent + c > cap:
                break
            lines.append(line); spent += c
        return lines, used + spent

    # --- LOCATE -----------------------------------------------------------
    def _build_locate(self, result: dict, budget: int) -> ContextResult:
        sym = result.get("symbol") or "?"
        refs = result.get("references", [])
        header = f"**{sym}** — used at:"
        used = self.counter.count(header)
        lines, uids, dropped = [header], [], 0
        for r in refs:
            line = (f"- {r['src_name']}  {r['relation']}  (conf {r.get('confidence', 0):.2f})  "
                    f"{r['src_uid'].split('::', 1)[0]}")
            c = self.counter.count(line)
            if used + c > int(budget * _SAFETY):
                dropped += 1
                continue
            lines.append(line); uids.append(r["src_uid"]); used += c
        if not refs:
            lines.append("(no references)")
        return ContextResult(text="\n".join(lines), cards=[{"uid": u} for u in uids], uids=uids,
                             used_tokens=used, budget_tokens=budget, dropped=dropped,
                             truncated=dropped > 0, intent="LOCATE")
