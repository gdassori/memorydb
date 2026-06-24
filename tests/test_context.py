"""ContextBuilder tests (context-builder-packing spec). Fully zero-dep (HeuristicCounter)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memorydb import ContextBuilder, ContextResult, HeuristicCounter  # noqa: E402


def _explain():
    return {
        "intent": "EXPLAIN",
        "seeds": [1, 2],
        "depths": {1: 0, 2: 0, 3: 1, 4: 2},
        "nodes": [
            {"id": 1, "uid": "a.py::send", "type": "function", "name": "send",
             "attrs": {"file_uid": "a.py", "start_line": 10, "signature": "def send(u, m):",
                       "docstring": "Send a notification."}},
            {"id": 2, "uid": "a.py::enqueue", "type": "function", "name": "enqueue",
             "attrs": {"file_uid": "a.py", "start_line": 20, "signature": "def enqueue(m):",
                       "docstring": "Queue it."}},
            {"id": 3, "uid": "b.py::Job", "type": "class", "name": "Job",
             "attrs": {"file_uid": "b.py", "start_line": 5, "signature": "class Job:"}},
            {"id": 4, "uid": "c.py::far", "type": "function", "name": "far",
             "attrs": {"file_uid": "c.py", "start_line": 1}},
        ],
        "edges": [
            {"src": "a.py::send", "dst": "a.py::enqueue", "relation": "CALLS", "confidence": 1.0},
            {"src": "b.py::Job", "dst": "a.py::send", "relation": "CALLS", "confidence": 0.97},
        ],
    }


def test_respects_budget():
    for budget in (40, 80, 200, 1000):
        res = ContextBuilder().build(_explain(), budget)
        assert res.used_tokens <= budget, (budget, res.used_tokens)


def test_ordering_seeds_and_score_first():
    res = ContextBuilder().build(_explain(), 1000)
    assert res.uids[0] in ("a.py::send", "a.py::enqueue")       # a seed ranks first
    if "c.py::far" in res.uids:                                 # the depth-2, edge-less node ranks last
        assert res.uids[-1] == "c.py::far"


def test_provenance_present():
    res = ContextBuilder().build(_explain(), 1000)
    locs = {"a.py::send": "a.py:10", "a.py::enqueue": "a.py:20", "b.py::Job": "b.py:5", "c.py::far": "c.py:1"}
    for uid in res.uids:
        assert locs[uid] in res.text                            # every card carries file:line


def test_deterministic():
    a = ContextBuilder().build(_explain(), 300)
    b = ContextBuilder().build(_explain(), 300)
    assert a.text == b.text and a.uids == b.uids and a.used_tokens == b.used_tokens


def test_reports_dropped():
    res = ContextBuilder().build(_explain(), 30)                # too small for all four cards
    assert res.dropped > 0 and res.truncated
    assert res.used_tokens <= 30 and len(res.uids) < 4          # explicit, not silently cut


def test_relationships_block_rendered():
    res = ContextBuilder().build(_explain(), 1000)
    assert "**Relationships**" in res.text
    assert "Job --CALLS--> send" in res.text                    # qualnames, an included edge


def test_locate_used_at_list():
    res = ContextBuilder().build({
        "intent": "LOCATE", "symbol": "send",
        "references": [{"src_uid": "b.py::Job.run", "src_name": "run", "relation": "CALLS", "confidence": 0.97}],
    }, 1000)
    assert res.intent == "LOCATE" and "used at:" in res.text and "run" in res.text


def test_empty_result():
    res = ContextBuilder().build({"intent": "EXPLAIN", "nodes": []}, 1000)
    assert isinstance(res, ContextResult) and res.text == "" and res.used_tokens == 0


def test_pluggable_token_counter():
    class WordCounter:
        def count(self, text):
            return max(1, len(text.split()))
    res = ContextBuilder(counter=WordCounter()).build(_explain(), 1000)
    # used_tokens is now a word count; sanity: it differs from the chars/4 heuristic and respects budget
    assert res.used_tokens <= 1000 and res.used_tokens != HeuristicCounter().count(res.text)


# --- PR #3 mega-review regressions -------------------------------------------

def test_pr3_1_invariant_degenerate_budgets():
    """used_tokens <= budget_tokens for ALL budgets, incl. zero/negative/tiny — EXPLAIN and LOCATE.
    LOCATE used to count its header unconditionally (overflow below header cost) and skip the
    non-negative clamp; EXPLAIN's first-card branch emitted a 1-token '#' fragment at budget 0."""
    locate = {"intent": "LOCATE", "symbol": "a_very_long_symbol_name_" * 4,
              "references": [{"src_uid": "b.py::Job.run", "src_name": "run", "relation": "CALLS",
                              "confidence": 0.9}]}
    for budget in (-5, 0, 1, 3, 5, 8, 20):
        e = ContextBuilder().build(_explain(), budget)
        assert e.used_tokens <= e.budget_tokens and e.budget_tokens >= 0, ("EXPLAIN", budget, e.used_tokens, e.budget_tokens)
        l = ContextBuilder().build(locate, budget)
        assert l.used_tokens <= l.budget_tokens and l.budget_tokens >= 0, ("LOCATE", budget, l.used_tokens, l.budget_tokens)


def test_pr3_2_single_oversized_card_flags_truncated():
    """One node whose card alone exceeds the budget is byte-cut in — that loss must be reported as
    truncated=True even though dropped=n-1=0 (the no-silent-truncation contract)."""
    big = {"intent": "EXPLAIN", "seeds": [1], "depths": {1: 0},
           "nodes": [{"id": 1, "uid": "a.py::f", "type": "function", "name": "f",
                      "attrs": {"file_uid": "a.py", "start_line": 1,
                                "signature": "def f():", "docstring": "x " * 5000}}]}
    res = ContextBuilder().build(big, 40)
    assert res.used_tokens <= 40
    assert res.truncated is True and len(res.uids) == 1     # cut in, but the cut is flagged
    assert len(res.text) < 5000                             # body really was sliced


def test_pr3_3_markdown_injection_neutralized():
    """Attacker-controlled signature/docstring/name cannot inject markdown structure (fake headers,
    code fences, phantom Relationships) into the EXPLAIN context."""
    evil = {"intent": "EXPLAIN", "seeds": [1], "depths": {1: 0},
            "nodes": [{"id": 1, "uid": "a.py::f", "type": "function", "name": "f",
                       "attrs": {"file_uid": "a.py", "start_line": 1,
                                 "signature": "def f()",
                                 "docstring": "ok\n### Injected Header\n```\n**Relationships**\nA --CALLS--> B"}}]}
    res = ContextBuilder().build(evil, 1000)
    lines = res.text.split("\n")
    # newline-collapse means the payload is inlined into the docstring paragraph — it cannot form a
    # NEW structural line: no forged header, no code fence, no standalone phantom-edge row.
    assert not any(ln.lstrip().startswith("### Injected") for ln in lines)   # no forged header line
    assert "```" not in res.text                             # no forged code fence
    assert "A --CALLS--> B" not in lines                     # not a standalone phantom-edge line


def test_pr3_4_cards_are_structured():
    """cards carries the structured form (name/type/file/line/signature/calls...), not just uid."""
    res = ContextBuilder().build(_explain(), 1000)
    assert res.cards and isinstance(res.cards[0], dict)
    c = next(c for c in res.cards if c["uid"] == "a.py::send")
    assert c["name"] == "send" and c["type"] == "function" and c["file"] == "a.py" and c["line"] == 10
    assert c["signature"] and set(("calls", "called_by")) <= set(c)


def test_pr3_7_locate_reference_line_sanitized():
    """A newline in src_name must not forge an extra reference row in the LOCATE 'used at' list."""
    res = ContextBuilder().build({
        "intent": "LOCATE", "symbol": "send",
        "references": [{"src_uid": "b.py::Job.run", "relation": "CALLS", "confidence": 0.9,
                        "src_name": "run\n- fake_caller  CALLS  (conf 1.00)  evil.py"}],
    }, 1000)
    rows = [ln for ln in res.text.split("\n") if ln.startswith("- ")]
    assert len(rows) == 1                          # exactly one real reference row (no forged extra row)
    assert "fake_caller" in rows[0]                # the payload is inlined into that row, not a new line
    assert "\n- fake_caller" not in res.text


# --- PR #3 re-review regressions (RR / Cn) -----------------------------------

def _node(uid, name="f", type="function", **attrs):
    a = {"file_uid": uid.split("::", 1)[0], "start_line": 1}
    a.update(attrs)
    return {"id": 1, "uid": uid, "type": type, "name": name, "attrs": a}


def test_rr_c7c9_empty_fields_no_lone_backslash():
    """_safe('') must stay '' — a card with no docstring/signature (the common case) must NOT render a
    lone '\\' or '`\\`' line (regression introduced by PR3-3's leading-marker escape)."""
    from memorydb.context import _safe
    assert _safe("") == "" and _safe(None) == "" and _safe("   ") == ""
    res = ContextBuilder().build({"intent": "EXPLAIN", "seeds": [1], "depths": {1: 0},
                                  "nodes": [_node("a.py::f")], "edges": []}, 2000)
    assert "\\" not in res.text and "`\\`" not in res.text, res.text
    assert res.text.splitlines() == ["### f  ·  function  ·  a.py:1"]   # header only, nothing spurious


def test_rr_c2_loc_filename_cannot_forge_header():
    """A newline (or markdown) in file_uid / the uid prefix must not forge a header/fence/section in
    the EXPLAIN card (the _loc path was unsanitized — re-review C2, same class as PR3-3)."""
    evil = {"intent": "EXPLAIN", "seeds": [1], "depths": {1: 0}, "edges": [],
            "nodes": [_node("x::f", file_uid="a.py\n### INJECTED\n```\n**Relationships**\nX --OWNS--> Y")]}
    text = ContextBuilder().build(evil, 2000).text
    lines = text.split("\n")
    assert not any(ln.strip() == "### INJECTED" for ln in lines)       # no forged header line
    assert "```" not in text                                          # no forged fence
    assert not any(ln.strip() == "X --OWNS--> Y" for ln in lines)     # no forged phantom edge row


def test_rr_c3_locate_src_file_cannot_forge_row():
    """A newline in the src_uid file part must not forge an extra reference row (PR3-7 covered
    src_name/relation but not src_file — re-review C3)."""
    res = ContextBuilder().build({"intent": "LOCATE", "symbol": "send", "references": [
        {"src_uid": "a.py\n- fake_caller  CALLS  (conf 1.00)  evil.py::Z.run",
         "src_name": "run", "relation": "CALLS", "confidence": 0.9}]}, 1000)
    rows = [ln for ln in res.text.split("\n") if ln.startswith("- ")]
    assert len(rows) == 1 and "fake_caller" in rows[0]                # inlined into the one real row


def test_rr2_safe_run_aware_no_snakecase_overreach():
    """A single leading '_'/'~' is NOT markdown structure — Python privates/dunders must pass through
    unescaped. (Leading '=' IS a setext-H1 underline and is escaped — see test_rr2_1.)"""
    from memorydb.context import _safe
    for ok in ("_private helper", "__init__ sets up", "__name__", "_x", "~tilde"):
        assert _safe(ok) == ok, ok                                   # no spurious leading backslash
    for bad in ("~~~ fence", "___ rule", "=== rule", "### h", "> q", "- l", "* i", "| t |"):
        assert _safe(bad).startswith("\\"), bad                      # genuine structure still escaped
    # end-to-end: a dunder docstring renders clean (the common Python case)
    res = ContextBuilder().build({"intent": "EXPLAIN", "seeds": [1], "depths": {1: 0}, "edges": [],
        "nodes": [_node("a.py::f", docstring="__init__ initializes the thing")]}, 2000)
    assert "__init__ initializes the thing" in res.text and "\\__init__" not in res.text


def test_rr3_1_relationships_edge_order_deterministic():
    """Two edges between the same pair at equal confidence but different relations must render in a
    stable, input-order-independent order — the sort key includes `relation` (RR3-1)."""
    base = {"intent": "EXPLAIN", "seeds": [1, 2], "depths": {1: 0, 2: 0},
            "nodes": [_node("m.py::A", name="A", type="class"),
                      dict(_node("m.py::B", name="B", type="class"), id=2)]}
    e_ic = [{"src": "m.py::A", "dst": "m.py::B", "relation": "INHERITS", "confidence": 1.0},
            {"src": "m.py::A", "dst": "m.py::B", "relation": "CALLS", "confidence": 1.0}]
    a = ContextBuilder().build({**base, "edges": e_ic}, 2000).text
    b = ContextBuilder().build({**base, "edges": list(reversed(e_ic))}, 2000).text
    assert a == b                                                    # same content, same render order
    # CALLS sorts before INHERITS (relation asc tiebreak) regardless of input order
    assert a.index("A --CALLS--> B") < a.index("A --INHERITS--> B")


def test_rr2_1_setext_equals_escaped_no_h1():
    """A leading '='/'==' is a setext-H1 underline — escaping it was dropped by the run-aware rewrite
    (RR2-1 regression). A '=' docstring under the signature line must not forge an <h1>."""
    from memorydb.context import _safe
    for s in ("=", "==", "= title", "=== rule"):
        assert _safe(s).startswith("\\"), s
    res = ContextBuilder().build({"intent": "EXPLAIN", "seeds": [1], "depths": {1: 0}, "edges": [],
        "nodes": [_node("m.py::imp", signature="def imp(x):", docstring="=")]}, 2000)
    assert not any(ln.strip() == "=" for ln in res.text.split("\n"))   # no bare setext underline


def test_rr2_2_spaced_rules_linkref_html_escaped_no_overreach():
    """Spaced thematic breaks, link-ref definitions and leading HTML are neutralized; snake_case,
    dunders and ordered lists (benign + common) are deliberately left alone (RR2-2)."""
    from memorydb.context import _safe
    for bad in ("_ _ _", "_ _ _ _ _", "- - -", "[ref]: http://x", "<div>", "<int> count"):
        assert _safe(bad).startswith("\\"), bad
    for ok in ("_private", "__init__", "__name__", "~tilde", "1. first step", "def f() -> int:"):
        assert _safe(ok) == ok, ok                                    # no over-reach
    res = ContextBuilder().build({"intent": "EXPLAIN", "seeds": [1], "depths": {1: 0}, "edges": [],
        "nodes": [_node("a.py::f", docstring="_ _ _")]}, 2000)
    assert not any(ln.strip() == "_ _ _" for ln in res.text.split("\n"))   # no forged <hr>


def test_rr2_2_safe_idempotent():
    """_safe must be idempotent — sym is _safe()'d then re-handled in the LOCATE fallback."""
    from memorydb.context import _safe
    for s in ("=", "_ _ _", "<div>", "### h", "_private", "", "normal text"):
        assert _safe(_safe(s)) == _safe(s), s


def test_rr2_3_explain_bytecut_balances_backticks():
    """The EXPLAIN single-card byte-cut must not leave a dangling unbalanced backtick (C8 was applied
    to LOCATE only — RR2-3). And used_tokens stays within budget."""
    big = {"intent": "EXPLAIN", "seeds": [1], "depths": {1: 0}, "edges": [],
           "nodes": [_node("a.py::f", signature="def f(" + "a" * 200 + ")")]}
    for b in (40, 50, 60, 80, 120):
        res = ContextBuilder().build(big, b)
        assert res.text.count("`") % 2 == 0, (b, res.text)            # balanced backticks
        assert res.used_tokens <= b


def test_rr_c4_tilde_fence_neutralized():
    """A docstring opening a ~~~ fence must be neutralized so it cannot swallow following cards."""
    evil = {"intent": "EXPLAIN", "seeds": [1, 2], "depths": {1: 0, 2: 0}, "edges": [],
            "nodes": [dict(_node("a.py::evil"), id=1, attrs={"file_uid": "a.py", "start_line": 1,
                                                             "docstring": "~~~ swallow everything"}),
                      dict(_node("a.py::safe", name="safe"), id=2)]}
    text = ContextBuilder().build(evil, 2000).text
    assert not any(ln.strip() == "~~~ swallow everything" for ln in text.split("\n"))  # escaped, not a fence
    assert "### safe" in text                                         # the second card is still visible


def test_rr_c6_calls_dedup_matches_card_and_dict():
    """The rendered '→ calls:' list and cards[].calls must dedupe identically (clip-then-set both)."""
    long_a, long_b = "p::" + "x" * 90 + "A", "p::" + "x" * 90 + "B"   # share first 80 chars after _qual
    res = ContextBuilder().build({"intent": "EXPLAIN", "seeds": [1], "depths": {1: 0},
        "nodes": [_node("a.py::caller")],
        "edges": [{"src": "a.py::caller", "dst": long_a, "relation": "CALLS", "confidence": 1.0},
                  {"src": "a.py::caller", "dst": long_b, "relation": "CALLS", "confidence": 1.0}]}, 4000)
    calls_line = next(ln for ln in res.text.split("\n") if "→ calls:" in ln)
    md_count = calls_line.count("…")                                  # clipped entries collapse to one
    assert res.cards[0]["calls"] == sorted(set(c for c in res.cards[0]["calls"]))
    assert md_count == len(res.cards[0]["calls"])                     # md list length == dict list length


def test_rr_c8_locate_header_overflow_no_unbalanced_markup():
    """At a tiny budget the LOCATE header must not emit a mid-token '**authen' fragment."""
    res = ContextBuilder().build({"intent": "LOCATE", "symbol": "authenticate_user_session",
                                  "references": []}, 3)
    assert res.used_tokens <= res.budget_tokens and res.truncated
    assert "**" not in res.text                                       # plain, no unbalanced bold marker


def test_rr_c10_budget_zero_drops_all_invariant_first():
    """budget 0/1 (card_budget<=0): drop all, dropped=n, truncated, empty text, used<=budget (the
    invariant beats 'always emit one card')."""
    for b in (0, 1):
        res = ContextBuilder().build({"intent": "EXPLAIN", "seeds": [1, 2], "depths": {1: 0, 2: 0},
            "nodes": [_node("a.py::f"), dict(_node("a.py::g", name="g"), id=2)], "edges": []}, b)
        assert res.used_tokens <= res.budget_tokens and res.dropped == 2 and res.truncated and res.text == ""


if __name__ == "__main__":
    tests = {n: f for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)}
    for name, fn in tests.items():
        try:
            fn()
            print(f"ok  {name}")
        except Exception as e:  # noqa
            import traceback
            print(f"FAIL {name}: {e}")
            traceback.print_exc()
    print("done")
