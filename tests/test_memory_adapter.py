"""MemoryAdapter tests (memory-adapter-agent-memory spec, TD-002). Zero-dep: HashingEmbedder + the
generic Store, so the substrate's second product runs with no extras installed."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memorydb import HashingEmbedder, Store  # noqa: E402
from memorydb import query as Q  # noqa: E402
from memorydb.adapters.memory import MemoryAdapter  # noqa: E402 (accessed via its path, like CodeAdapter)


def _mem():
    return MemoryAdapter(Store(":memory:"), HashingEmbedder(dim=64))


def _fact_count(store) -> int:
    return store.conn.execute("SELECT COUNT(*) FROM nodes WHERE type = 'Fact'").fetchone()[0]


# --- writes & linking -----------------------------------------------------------------------------------

def test_remember_creates_links():
    m = _mem()
    fid = m.remember("Guido moved to Bangkok in 2024", kind="semantic", entities=["Guido", "Bangkok"])
    node = m.store.get_nodes([fid])[0]
    assert node["type"] == "Fact" and node["attrs"]["tier"] == "semantic"
    assert node["body"] == "Guido moved to Bangkok in 2024"
    incoming = {(r["src_name"], r["relation"]) for r in Q.references_to(m.store, node["uid"])}
    assert ("Guido", "ABOUT") in incoming and ("Bangkok", "ABOUT") in incoming   # entity --ABOUT--> Fact
    m.store.close()


def test_entity_idempotent_by_normalized_name():
    m = _mem()
    assert m.entity("Guido") == m.entity("  guido ")          # normalized -> one node
    assert m.store.conn.execute("SELECT COUNT(*) FROM nodes WHERE type='Entity'").fetchone()[0] == 1
    m.store.close()


def test_relate_entities_auto_creates_unknown_at_low_confidence():
    m = _mem()
    m.relate("Guido", "WORKS_ON", "Spruned")                 # both unknown -> auto-created
    incoming = Q.references_to(m.store, "Spruned")
    assert any(r["src_name"] == "Guido" and r["relation"] == "WORKS_ON" for r in incoming)
    guido = m.store.get_nodes([m.store.id_for("entity::guido")])[0]
    assert guido["type"] == "Entity" and guido["confidence"] == pytest.approx(0.5)   # auto -> low conf
    # an explicit entity() upgrades the auto-created node to full confidence (single keyed path)
    m.entity("Guido")
    assert m.store.get_nodes([m.store.id_for("entity::guido")])[0]["confidence"] == pytest.approx(1.0)
    m.store.close()


# --- tiers ----------------------------------------------------------------------------------------------

def test_dedupe_semantic_reinforces_confidence():
    m = _mem()
    a = m.remember("Guido created Spruned", kind="semantic", confidence=0.5)
    b = m.remember("Guido created Spruned", kind="semantic", confidence=0.5)   # same text
    assert a == b and _fact_count(m.store) == 1                # one node, not duplicated
    assert m.store.get_nodes([a])[0]["confidence"] > 0.5       # reinforced toward 1.0 (0.75)
    m.store.close()


def test_contradiction_keeps_both():
    m = _mem()
    m.remember("Guido lives in Bangkok", kind="semantic", entities=["Guido"])
    m.remember("Guido lives in Italy", kind="semantic", entities=["Guido"])   # different text
    assert _fact_count(m.store) == 2                           # both kept, not overwritten (TD-008/009)
    m.store.close()


def test_episodic_distinct_by_time():
    m = _mem()
    a = m.remember("Guido said hi", kind="episodic", at="2024-01-01")
    b = m.remember("Guido said hi", kind="episodic", at="2024-02-01")   # same text, different time
    c = m.remember("Guido said hi", kind="episodic", at="2024-01-01")   # identical event -> dedupe
    assert a != b and a == c
    epi = m.store.get_nodes([a])[0]
    assert epi["type"] == "Episode" and epi["valid_from"] == "2024-01-01"
    m.store.close()


def test_procedural_steps_ordered():
    m = _mem()
    m.remember("Deploy service", kind="procedural", steps=["build the image", "run tests", "ship to prod"])
    assert m.steps_of("Deploy service") == ["build the image", "run tests", "ship to prod"]
    assert m.steps_of("procedure::deadbeef") == [] and m.steps_of("nonexistent") == []   # unknown -> []
    m.store.close()


def test_unknown_kind_raises():
    m = _mem()
    with pytest.raises(ValueError, match="kind"):
        m.remember("x", kind="bogus")
    m.store.close()


# --- recall ---------------------------------------------------------------------------------------------

def test_recall_via_entity_subgraph():
    m = _mem()
    m.remember("Guido moved to Bangkok in 2024", kind="semantic", entities=["Guido", "Bangkok"])
    m.remember("the weather is nice today", kind="episodic", at="2024-06-01")   # unrelated noise
    res = m.recall("Guido")
    assert res["seeds"], "vector seed should find the Guido entity / fact"
    facts = [n for n in res["nodes"] if n["type"] == "Fact"]
    assert facts and "Bangkok" in facts[0]["body"]            # the fact reached through its entity graph
    m.store.close()


def test_recall_kinds_filter_excludes_other_tiers():
    m = _mem()
    m.remember("Guido created Spruned", kind="semantic", entities=["Guido"])
    m.remember("Deploy Guido service", kind="procedural", steps=["a", "b"])
    # restrict the seed to semantic only -> a Procedure must not be a vector seed.
    res = m.recall("Guido", kinds=("semantic",))
    seed_types = {m.store.get_nodes([s])[0]["type"] for s in res["seeds"]}
    assert "Procedure" not in seed_types
    m.store.close()


def test_recall_empty_store_is_safe():
    m = _mem()
    res = m.recall("anything")
    assert res["seeds"] == [] and res["nodes"] == [] and res["edges"] == []
    m.store.close()
