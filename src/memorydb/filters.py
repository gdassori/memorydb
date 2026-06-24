"""Allowlisted FILTER → parameterized SQL (llm-intent-classifier spec; TD-007).

The classifier's ``filters`` dict is LLM-/attacker-influenced, so every key is checked against a fixed
allowlist and every value is **bound**, never string-interpolated — an injection payload in a value is
inert (it becomes a literal bind, not SQL). Unknown or empty keys are dropped and returned for logging.

``since`` is compared against the file node's ``attrs.mtime``. The indexer stamps ``mtime`` as an epoch
number (``os.path.getmtime``), so a ``since`` date/datetime is coerced to a float epoch here — the
comparison is numeric and the value stays bound (the spec's ISO-8601 remediation predates the indexer's
epoch storage; we adapt to the shipped data instead of forcing a re-index).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

# key -> (predicate with a :key bind placeholder, needs the file-node join). Order here is the canonical
# predicate order, so the built SQL/params are deterministic regardless of the input dict's key order.
# path_glob matches the owning FILE path (n.file_uid), NOT n.uid — the uid carries a `::qualname` suffix,
# so a file-anchored glob like "pkg/queue/*.py" would never match against the uid (re-review P4-6).
_ALLOWED = {
    "type": ("n.type = :type", False),
    "lang": ("json_extract(n.attrs, '$.lang') = :lang", False),
    "path_glob": ("n.file_uid GLOB :path_glob", False),
    "since": ("json_extract(f.attrs, '$.mtime') >= :since", True),
}


# Explicit `since`-string grammar, parsed with strptime so it is IDENTICAL across interpreters.
# datetime.fromisoformat widened its accepted forms in 3.11 (Z-suffix, basic format, week dates), which
# would make the same string parse on 3.11+ but raise on 3.10 — a version-dependent FILTER result set
# (re-review P4R-2). strptime behaves the same on 3.10/3.11/3.12; a trailing 'Z' is normalized first.
_SINCE_FORMATS = (
    "%Y-%m-%d", "%Y%m%d",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M%z",           # minute precision (no seconds) — re-review P4R3-1
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S%z",
    "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M%z",
)


def _to_epoch(value) -> Optional[float]:
    """Coerce a ``since`` value to a float epoch matching the indexer's stored ``attrs.mtime``
    (``os.path.getmtime`` → epoch seconds). A numeric *type* is taken as an epoch; a *string* is parsed
    with an explicit, interpreter-independent date/datetime grammar — never as a bare epoch, so a
    year-only string like ``"2026"`` is rejected rather than read as ``2026.0`` (~1970) and silently
    matching everything (re-review P4-2). Returns ``None`` (→ predicate dropped) for a non-finite or
    over-range number (an arbitrary-precision int can overflow ``float`` — re-review P4R-1) or anything
    unparseable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):           # a real epoch number
        try:
            f = float(value)                      # a huge int overflows float -> drop, do not raise
        except (OverflowError, ValueError):
            return None
        return f if math.isfinite(f) else None
    s = str(value).strip()
    if s[-1:] in ("Z", "z"):                      # normalize the common LLM UTC suffix for %z
        s = s[:-1] + "+00:00"
    for fmt in _SINCE_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    else:
        return None                               # not a recognized date/datetime -> drop
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def build_filter_query(filters: dict, *, limit: Optional[int] = None):
    """Build ``(sql, params, dropped_keys)`` for a FILTER intent.

    ``sql`` selects matching symbol-node ids (``n.id``, file nodes excluded), ordered by ``uid`` for
    determinism, with every value bound in ``params``. ``sql`` is ``None`` when no usable predicate
    remains (e.g. all keys unknown/empty). ``dropped_keys`` lists keys the caller should log.
    """
    filters = filters or {}
    preds: list[str] = []
    params: dict = {}
    dropped: list[str] = []
    needs_join = False
    for key, (predicate, join) in _ALLOWED.items():     # fixed order -> deterministic SQL & params
        if key not in filters:
            continue
        value = filters[key]
        if value is None or value == "":
            dropped.append(key)
            continue
        # A bind value MUST be a scalar — sqlite3 raises ProgrammingError on a list/dict, which would
        # escape MemoryDB.ask() (the LLM can return either). Drop non-scalars like unknown keys (P4-1).
        if not isinstance(value, (str, int, float)):
            dropped.append(key)
            continue
        if key == "since":
            value = _to_epoch(value)
            if value is None:
                dropped.append(key)
                continue
        params[key] = value
        preds.append(predicate)
        needs_join = needs_join or join
    dropped.extend(k for k in filters if k not in _ALLOWED)   # unknown keys
    if not preds:
        return None, {}, dropped
    # LEFT JOIN so the recency predicate (not the join) decides membership: a symbol with an orphan/
    # missing file node or an unknown mtime fails `mtime >= :since` explicitly rather than vanishing at
    # the join. `since` therefore returns only confirmed-recent symbols (unknown recency is excluded by
    # design — a recency filter cannot vouch for an unknown mtime) (re-review P4-3).
    join_sql = " LEFT JOIN nodes f ON f.uid = n.file_uid AND f.type = 'file'" if needs_join else ""
    sql = ("SELECT n.id FROM nodes n" + join_sql
           + " WHERE n.type != 'file' AND " + " AND ".join(preds)
           + " ORDER BY n.uid")
    if limit is not None:
        sql += " LIMIT :__limit"
        params["__limit"] = int(limit)
    return sql, params, dropped
