"""``memorydb-eval`` — run a retrieval-quality suite or compare two scorecards (eval-harness spec).

Thin argparse wrapper over the Evaluator: ``run`` indexes a suite fixture and prints/writes a
scorecard; ``compare`` diffs two saved scorecards so ranking changes can be judged run-over-run.
Same exit contract as the main CLI: 0 ok, 1 usage error, 2 runtime error.
"""
from __future__ import annotations

import json
import sys
from typing import Optional

from ..cli import _Parser, _resolve_embedder
from . import Scorecard, compare, evaluate_suite


def _build_parser() -> _Parser:
    p = _Parser(prog="memorydb-eval", description="MemoryDB retrieval-quality evaluation.")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    s = sub.add_parser("run", help="index a suite fixture and score its labeled cases")
    s.add_argument("suite", help="path to a suite dir (with repo/ and cases.jsonl)")
    s.add_argument("-k", type=int, default=10, help="cutoff for recall@k / nDCG (default 10)")
    s.add_argument("--embedder", metavar="module:attr", help="dotted path to an Embedder (default: hashing)")
    s.add_argument("--json", metavar="OUT", help="also write the scorecard JSON to OUT")
    s.set_defaults(func=_cmd_run)

    s = sub.add_parser("compare", help="diff two scorecard JSON files (new vs baseline)")
    s.add_argument("baseline")
    s.add_argument("new")
    s.set_defaults(func=_cmd_compare)
    return p


def main(argv: Optional[list] = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    if not getattr(args, "command", None):
        parser.print_help(sys.stderr)
        return 1
    try:
        return args.func(args)
    except Exception as e:  # runtime error -> 2, clean message
        print(f"error: {e}", file=sys.stderr)
        return 2


def _cmd_run(args) -> int:
    embedder, _ = _resolve_embedder(args.embedder)
    card = evaluate_suite(args.suite, embedder=embedder, k=args.k)
    _render_scorecard(card)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(card.to_dict(), fh, indent=2)
        print(f"\nwrote {args.json}", file=sys.stderr)
    return 0


def _cmd_compare(args) -> int:
    base = Scorecard.from_dict(_load_json(args.baseline))
    new = Scorecard.from_dict(_load_json(args.new))
    deltas = compare(base, new)
    print(f"compare (new − baseline): {args.new}  vs  {args.baseline}")
    for group in ("locate", "explain"):
        print(f"  {group}:")
        for metric, delta in deltas[group].items():
            arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "·")
            print(f"    {metric:<16} {arrow} {delta:+.4f}")
    return 0


def _load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _render_scorecard(card: Scorecard) -> None:
    loc, exp = card.locate, card.explain
    print(f"LOCATE  (n={loc.get('n', 0)})")
    print(f"  precision {loc.get('precision', 0):.3f}  precision@≥{0.9} {loc.get('precision_high', 0):.3f}  "
          f"recall {loc.get('recall', 0):.3f}  f1 {loc.get('f1', 0):.3f}")
    print(f"EXPLAIN  (n={exp.get('n', 0)}, k={card.k})")
    print(f"  recall@k {exp.get('recall_at_k', 0):.3f}  mrr {exp.get('mrr', 0):.3f}  "
          f"ndcg {exp.get('ndcg', 0):.3f}")
    if card.broken:
        print(f"\n⚠ {len(card.broken)} broken case(s) excluded (expected uid not in index): "
              + ", ".join(card.broken), file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
