"""stdlib-only REDI-02 validation runner. Mirrors REDI-01 pattern.

No pytest required. Run via import-mode to avoid caching paradox:
    python -m scripts.run_redi_02_validations
"""

from __future__ import annotations

import os
import sqlite3
import sys
import io
import traceback
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

# Force UTF-8 on stdout for cross-platform consistency (avoid Windows cp1252 issues with unicode)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


PASS = 0
FAIL = 0
RESULTS: list[tuple[str, bool, str]] = []


def check(name, fn):
    global PASS, FAIL
    try:
        fn()
        PASS += 1
        RESULTS.append((name, True, ""))
        print(f"  PASS {name}")
    except AssertionError as e:
        FAIL += 1
        msg = str(e)[:200]
        RESULTS.append((name, False, msg))
        print(f"  FAIL {name}: assertion: {msg}")
    except Exception as e:  # noqa: BLE001
        FAIL += 1
        tb = traceback.format_exc(limit=3)
        RESULTS.append((name, False, f"{type(e).__name__}: {e}; {tb!s:.300}"))
        print(f"  FAIL {name}: {type(e).__name__}: {e}")


def _mem():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    from redi_lab.persistence import bootstrap_schema, check_schema_version
    bootstrap_schema(c)
    check_schema_version(c)
    return c


def s01_schema_bootstrap():
    conn = _mem()
    tables = sorted(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall())
    assert tables == [
        "evidence_entries", "experiment_manifests", "factory_runs",
        "persistence_schema_metadata", "pricing_evidence",
    ], tables
    from redi_lab.persistence import check_schema_version
    assert check_schema_version(conn) == "1"
    # FK pragma
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def s02_unsupported_schema_version():
    p = REPO_ROOT / ".redi02_tmp_bad.db"
    if p.exists():
        p.unlink()
    try:
        c = sqlite3.connect(str(p))
        c.row_factory = sqlite3.Row
        from redi_lab.persistence import bootstrap_schema, check_schema_version
        bootstrap_schema(c)
        c.execute("UPDATE persistence_schema_metadata SET value='999' WHERE key='schema_version'")
        c.commit()
        c.close()
        c2 = sqlite3.connect(str(p))
        try:
            check_schema_version(c2)
            raise AssertionError("expected UnsupportedPersistenceSchemaError")
        except Exception as e:  # noqa: BLE001
            name = type(e).__name__
            assert "UnsupportedPersistenceSchemaError" == name, name
        finally:
            c2.close()
    finally:
        # Retry loop to handle Windows file-lock transient races
        for _ in range(5):
            try:
                if p.exists():
                    p.unlink()
                break
            except PermissionError:
                import time
                time.sleep(0.05)


def s03_ledger_append_event_types():
    from redi_lab.domain import LifecycleEventType, LifecycleEvent, new_id
    from redi_lab.evidence import append_event
    conn = _mem()
    # Insert dummy experiment + factory run with explicit values for NOT NULL columns
    mid = "E_STD_DUMMY_" + new_id("experiment").split("_", 1)[1]
    rid = "R_STD_DUMMY_" + new_id("factory_run").split("_", 1)[1]
    ts_iso = "2026-09-15T12:00:00+00:00"
    conn.execute(
        """INSERT INTO experiment_manifests
           (id, title, hypothesis_reference, schema_version, payload_json, payload_sha256, preregistered_at, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (mid, "std_dummy_title", "H_STD", 1, "{}",
         "a" * 64, ts_iso, ts_iso),
    )
    import json
    fr_payload = json.dumps({"__type__": "FactoryRun", "id": rid, "schema_version": 1}, sort_keys=True)
    import hashlib
    fr_sha = hashlib.sha256(fr_payload.encode()).hexdigest()
    conn.execute(
        """INSERT INTO factory_runs
           (id, product_spec_id, dag_id, recipe_id, experiment_id, mode, final_status, verified, payload_json, payload_sha256, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (rid, "P_STD", "D_STD", None, mid, "exploration", "pending", 0, fr_payload, fr_sha, ts_iso),
    )
    conn.commit()
    from redi_lab.persistence import EvidenceLedger
    ledger = EvidenceLedger(conn)
    for et in list(LifecycleEventType):
        ev = LifecycleEvent(
            event_type=et, timestamp=datetime(2026, 9, 15, tzinfo=timezone.utc),
            entity_ids={"factory_run_id": rid, "experiment_id": mid},
        )
        meta = append_event(ledger, ev, factory_run_id=rid, experiment_id=mid)
        assert meta["entity_type"] == "LifecycleEvent"
    assert ledger.count() == len(list(LifecycleEventType)) == 18


def s04_ledger_none_preservation_decimal_datetime():
    from redi_lab.domain import RunEvidence, MonetaryCost, Timing, EvidenceId, new_id
    from redi_lab.evidence import append_run_evidence
    from redi_lab.domain import domain_from_json, SCHEMA_VERSION
    conn = _mem()
    from redi_lab.persistence import EvidenceLedger
    ledger = EvidenceLedger(conn)
    amount = Decimal("1.23456789012345678901")
    t = datetime(2026, 9, 15, 12, 30, 45, 123456, tzinfo=timezone.utc)
    eid = EvidenceId("re_check_std_" + new_id("evidence").split("_", 1)[1])
    re_obj = RunEvidence(
        id=eid, schema_version=SCHEMA_VERSION,
        experiment_id=None, factory_run_id=None, product_id=None, product_class_id=None,
        recipe_id=None, worker_id=None, token_usage=None,
        timing=Timing(started_at=t, ended_at=t, wall_clock_seconds=0.001, compute_seconds=0.001),
        cost=MonetaryCost(amount=amount, currency="USD", basis="billed"),
        final_status=None, failure_category=None, created_at=t,
    )
    append_run_evidence(ledger, re_obj)
    row = ledger.query_by_entity_id(str(eid))
    assert row is not None
    back = domain_from_json(row["payload_json"])
    assert back.experiment_id is None
    assert back.factory_run_id is None
    assert back.worker_id is None
    assert back.token_usage is None
    assert back.cost.amount == amount
    assert str(back.cost.amount) == "1.23456789012345678901"
    assert back.timing.started_at.isoformat() == t.isoformat()
    assert back.created_at.isoformat() == t.isoformat()


def s05_manifest_freeze_amend():
    from redi_lab.domain import ExperimentManifest, ExperimentId, new_id, domain_to_json
    from redi_lab.persistence import ExperimentRepository, EvidenceLedger
    from redi_lab.persistence.exceptions import AlreadyFrozenError, NotFrozenError
    conn = _mem()
    ledger = EvidenceLedger(conn)
    repo = ExperimentRepository(conn, ledger)
    def m():
        return ExperimentManifest(
            id=ExperimentId(new_id("experiment")), schema_version=1,
            title="std test", hypothesis_reference="H1",
            preregistered_at=datetime(2026,9,15,tzinfo=timezone.utc),
            created_at=datetime(2026,9,15,tzinfo=timezone.utc),
        )
    orig = m()
    repo.store_preregistered(orig)
    repo.freeze(str(orig.id))
    try:
        repo.freeze(str(orig.id))
        raise AssertionError("expected AlreadyFrozenError")
    except AlreadyFrozenError:
        pass
    # Amend on unfrozen
    unfrozen = m()
    repo.store_preregistered(unfrozen)
    try:
        repo.amend(str(unfrozen.id), m())
        raise AssertionError("expected NotFrozenError")
    except NotFrozenError:
        pass
    # Valid amend
    new_id_str = repo.amend(str(orig.id), m())
    revs = repo.list_revisions(str(orig.id))
    assert len(revs) == 2
    back = repo.read(str(orig.id))
    assert domain_to_json(back) == domain_to_json(orig), "original mutated after amend"


def s06_factory_run_store_fk_close():
    from redi_lab.domain import (
        ExperimentManifest, ExperimentId, FactoryRun, FactoryRunId,
        ProductId, Mode, RunStatus, new_id, domain_to_json,
    )
    from redi_lab.persistence import (
        EvidenceLedger, ExperimentRepository, FactoryRunRepository,
        IncompleteEvidenceError, ClosedRunError,
    )
    conn = _mem()
    ledger = EvidenceLedger(conn)
    er = ExperimentRepository(conn, ledger)
    fr = FactoryRunRepository(conn, ledger)
    mid = ExperimentId(new_id("experiment"))
    exp = ExperimentManifest(
        id=mid, schema_version=1, title="T", hypothesis_reference="H1",
        preregistered_at=datetime(2026,9,15,tzinfo=timezone.utc),
        created_at=datetime(2026,9,15,tzinfo=timezone.utc),
    )
    er.store_preregistered(exp)
    rid = FactoryRunId(new_id("factory_run"))
    pid = ProductId(new_id("product"))
    run = FactoryRun(
        id=rid, schema_version=1,
        product_spec_id=pid,
        mode=Mode.EXPLORATION,
        final_status=RunStatus.PENDING,
    )
    fr.store(run, experiment_id=str(mid))
    # bad FK
    bad = FactoryRun(
        id=FactoryRunId(new_id("factory_run")), schema_version=1,
        product_spec_id=ProductId(new_id("product")),
        mode=Mode.EXPLORATION,
        final_status=RunStatus.PENDING,
    )
    try:
        fr.store(bad, experiment_id="DOES_NOT_EXIST_999")
        raise AssertionError("expected IncompleteEvidenceError on FK fail")
    except IncompleteEvidenceError:
        pass
    # close once
    fr.close_run(str(rid), RunStatus.COMPLETED, True)
    try:
        fr.close_run(str(rid), RunStatus.COMPLETED, True)
        raise AssertionError("expected ClosedRunError")
    except ClosedRunError:
        pass
    back = fr.read(str(rid))
    assert back.mode == Mode.EXPLORATION


def s07_duplicate_explicit_reject():
    from redi_lab.domain import LifecycleEvent, EventId, LifecycleEventType
    from redi_lab.evidence import append_event
    from redi_lab.persistence import EvidenceLedger, DuplicateLedgerEntryError
    conn = _mem()
    ledger = EvidenceLedger(conn)
    same = EventId("dup_id_stdlib")
    ts = datetime(2026,9,15,tzinfo=timezone.utc)
    append_event(ledger, LifecycleEvent(id=same, event_type=LifecycleEventType.GATE_PASSED, timestamp=ts, payload={}))
    try:
        append_event(ledger, LifecycleEvent(id=same, event_type=LifecycleEventType.GATE_FAILED, timestamp=ts, payload={}))
        raise AssertionError("expected DuplicateLedgerEntryError")
    except DuplicateLedgerEntryError:
        pass


def s08_no_update_delete_apis():
    from redi_lab.persistence import EvidenceLedger, ExperimentRepository, FactoryRunRepository
    bad = ("update", "delete", "remove")
    for cls in (EvidenceLedger, ExperimentRepository, FactoryRunRepository):
        for name in dir(cls):
            if name.startswith("_"):
                continue
            low = name.lower()
            for b in bad:
                assert b not in low, f"{cls.__name__}.{name} suggests mutation"


def s09_sql_update_delete_grep():
    forbidden_paths = [
        REPO_ROOT / "redi_lab" / "persistence",
        REPO_ROOT / "redi_lab" / "evidence",
        REPO_ROOT / "redi_lab" / "replay",
    ]
    allowed_tokens = ("frozen_ts", "closed_at")
    for d in forbidden_paths:
        for f in sorted(d.glob("*.py")):
            lines = f.read_text(encoding="utf-8").splitlines()
            for i, ln in enumerate(lines, start=1):
                s = ln.split("#", 1)[0]
                # Must be inside a Python string literal to count as embedded SQL
                if '"' not in s and "'" not in s:
                    continue
                for tok in ("UPDATE ", "DELETE "):
                    if tok in s and not any(a in s for a in allowed_tokens):
                        raise AssertionError(f"unexpected {tok.strip()} at {f}:{i}: {s[:160]}")


def s10_transaction_rollback():
    from redi_lab.domain import LifecycleEvent, EventId, LifecycleEventType
    from redi_lab.persistence import EvidenceLedger, AppendRequest, DuplicateLedgerEntryError
    conn = _mem()
    ledger = EvidenceLedger(conn)
    ts = datetime(2026,9,15,tzinfo=timezone.utc)
    eid = EventId("batch_ok_stdlib")
    first = LifecycleEvent(id=eid, event_type=LifecycleEventType.GATE_PASSED, timestamp=ts, payload={})
    dup = LifecycleEvent(id=eid, event_type=LifecycleEventType.GATE_FAILED, timestamp=ts, payload={})
    before = ledger.count()
    try:
        ledger.append_batch([{"entity": first}, {"entity": dup}])
        raise AssertionError("expected DuplicateLedgerEntryError from batch")
    except DuplicateLedgerEntryError:
        pass
    after = ledger.count()
    assert before == after, f"batch rollback failed {before} vs {after}"


def s11_sequence_order():
    from redi_lab.domain import LifecycleEvent, EventId, LifecycleEventType
    from redi_lab.evidence import append_event
    from redi_lab.persistence import EvidenceLedger
    conn = _mem()
    ledger = EvidenceLedger(conn)
    ts = datetime(2026,9,15,tzinfo=timezone.utc)
    order = []
    for i in range(5):
        ev = LifecycleEvent(id=EventId(f"seq_std_{i}"), event_type=LifecycleEventType.WORKER_STARTED, timestamp=ts, payload={"i": i})
        meta = append_event(ledger, ev)
        order.append(meta["entity_id"])
    rows = ledger.query_all("ASC")
    assert [r["entity_id"] for r in rows] == order
    seqs = [r["global_sequence"] for r in rows]
    assert seqs == sorted(seqs)
    assert all(b - a == 1 for a, b in zip(seqs[:-1], seqs[1:]))


def s12_payload_hashes_ok_on_fixture():
    from tests.unit._redi02_fixtures import build_deterministic_ledger
    from redi_lab.persistence import payload_hash_from_json, canonical_hash
    from redi_lab.domain import domain_from_json
    conn, _, _, _ = build_deterministic_ledger()
    for r in conn.execute("SELECT entity_id, payload_json, payload_sha256 FROM evidence_entries ORDER BY global_sequence"):
        actual1 = payload_hash_from_json(r["payload_json"])
        assert actual1 == r["payload_sha256"], (r["entity_id"], actual1, r["payload_sha256"])
        obj = domain_from_json(r["payload_json"])
        assert canonical_hash(obj) == r["payload_sha256"], (r["entity_id"],)


def s13_integrity_chain_and_tamper():
    from redi_lab.persistence.integrity import verify_chain, compute_prev_hash
    h1 = "a" * 64
    h2 = "b" * 64
    ph1 = compute_prev_hash(1, h1)
    assert verify_chain(None, None, 1, h1, None)
    assert verify_chain(1, h1, 2, h2, ph1)
    assert not verify_chain(1, h1, 2, h2, "0" * 64)


def s14_env_capture_no_secrets():
    from redi_lab.persistence import capture_repro_environment
    from redi_lab.persistence.env_capture import _BLOCKLIST_SUBSTRINGS
    env = capture_repro_environment()
    assert env.python_version and env.os_description
    for k in list(env.metadata.keys()):
        up = k.upper()
        for b in _BLOCKLIST_SUBSTRINGS:
            assert b not in up, k
    try:
        capture_repro_environment(extra={"MY_TOKENS_X": 1})
        raise AssertionError("expected ValueError blocklist defense")
    except ValueError as e:
        assert "blocklist" in str(e).lower()


def s15_replay_on_fixture():
    from tests.unit._redi02_fixtures import build_deterministic_ledger
    from redi_lab.replay import ReplayReader
    from redi_lab.domain import LifecycleEventType, RunStatus
    conn, _, run, _ = build_deterministic_ledger()
    reader = ReplayReader(conn)
    res = reader.replay_factory_run(run.id)
    assert res.integrity_status == "OK"
    assert res.final_verification_state is True
    assert len(res.ordered_events) == 6
    types_back = [e.event_type for e in res.ordered_events]
    assert LifecycleEventType.PRODUCT_VERIFIED in types_back


def s16_replay_double_equivalent():
    from tests.unit._redi02_fixtures import build_deterministic_ledger
    from redi_lab.replay import ReplayReader
    conn, _, run, _ = build_deterministic_ledger()
    rr = ReplayReader(conn)
    r1 = rr.replay_factory_run(run.id)
    r2 = rr.replay_factory_run(run.id)
    assert r1.integrity_status == r2.integrity_status
    assert [e.id for e in r1.ordered_events] == [e.id for e in r2.ordered_events]
    assert r1.gate_outcomes == r2.gate_outcomes
    assert r1.final_verification_state == r2.final_verification_state


def s17_replay_incomplete_rejects():
    from tests.unit._redi02_fixtures import build_deterministic_ledger
    from redi_lab.replay import ReplayReader
    from redi_lab.persistence import IncompleteEvidenceError
    conn, manifest, run, _ = build_deterministic_ledger()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DELETE FROM experiment_manifests WHERE id=?", (str(manifest.id),))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    rr = ReplayReader(conn)
    try:
        rr.replay_factory_run(run.id)
        raise AssertionError("expected IncompleteEvidenceError")
    except IncompleteEvidenceError:
        pass


def s18_replay_tamper_detect():
    from tests.unit._redi02_fixtures import build_deterministic_ledger
    from redi_lab.replay import ReplayReader
    import hashlib
    conn, _, run, _ = build_deterministic_ledger()
    row = conn.execute("SELECT entry_id FROM evidence_entries ORDER BY global_sequence LIMIT 1").fetchone()
    bad_json = '{"__type__": "LifecycleEvent", "id": "tampered_x", "tampered": true}'
    conn.execute(
        "UPDATE evidence_entries SET payload_json = ?, payload_sha256 = ? WHERE entry_id = ?",
        (bad_json, hashlib.sha256(bad_json.encode()).hexdigest(), row[0]),
    )
    conn.commit()
    rr = ReplayReader(conn)
    res = rr.replay_factory_run(run.id)
    assert res.integrity_status in ("TAMPERED", "INCOMPLETE_CHAIN")


def s19_domain_zero_sqlite3_imports():
    import ast
    dom_root = REPO_ROOT / "redi_lab" / "domain"
    forbidden = ("sqlite3", "sqlalchemy", "openai", "gemini", "anthropic", "ollama", "astra", "redis")
    for f in sorted(dom_root.glob("*.py")):
        src = f.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(f))
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    low = a.name.lower()
                    for fb in forbidden:
                        if fb in low:
                            raise AssertionError(f"{f.name} ast Import {a.name}")
            if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                low = (n.module or "").lower()
                for fb in forbidden:
                    if fb in low:
                        raise AssertionError(f"{f.name} ast ImportFrom {n.module}")
    # reverse-layer check
    forbidden_layers = ("redi_lab.persistence", "redi_lab.evidence", "redi_lab.replay")
    for f in sorted(dom_root.glob("*.py")):
        src = f.read_text(encoding="utf-8")
        for tok in forbidden_layers:
            if tok in src:
                raise AssertionError(f"{f.name} string ref to Layer C module: {tok}")


def s20_pricing_decimal_preserved():
    from redi_lab.persistence import make_pricing_evidence_row, insert_pricing_evidence
    conn = _mem()
    amount = Decimal("0.0009876543210123456789")
    row = make_pricing_evidence_row(amount, "USD", source_ref="std_test", opaque_provider_ref=None, schedule_version="v1")
    pk = insert_pricing_evidence(conn, row)
    back = conn.execute("SELECT amount_decimal_str FROM pricing_evidence WHERE id=?", (pk,)).fetchone()
    assert Decimal(back[0]) == amount
    assert str(back[0]) == "0.0009876543210123456789"


def s21_fixture_build_and_counts():
    from tests.unit._redi02_fixtures import build_deterministic_ledger
    conn, manifest, run, events = build_deterministic_ledger()
    assert len(events) == 6
    n_entries = conn.execute("SELECT COUNT(*) FROM evidence_entries").fetchone()[0]
    assert n_entries == 8, n_entries  # 6 events + 1 RE + 1 GR
    assert conn.execute("SELECT COUNT(*) FROM factory_runs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM experiment_manifests").fetchone()[0] == 1


def s22_exception_inheritance():
    from redi_lab.persistence import exceptions as ex
    assert issubclass(ex.UnsupportedPersistenceSchemaError, ValueError)
    assert issubclass(ex.DuplicateLedgerEntryError, ValueError)
    assert issubclass(ex.AlreadyFrozenError, RuntimeError)
    assert issubclass(ex.NotFrozenError, RuntimeError)
    assert issubclass(ex.ClosedRunError, RuntimeError)
    assert issubclass(ex.IncompleteEvidenceError, ValueError)
    assert issubclass(ex.MalformedPayloadError, ValueError)


def s23_disallow_non_dataclass_in_ledger():
    from redi_lab.persistence import EvidenceLedger
    conn = _mem()
    ledger = EvidenceLedger(conn)
    class NotADataclass: pass
    try:
        ledger.append(NotADataclass())  # type: ignore[arg-type]
        raise AssertionError("TypeError expected")
    except TypeError:
        pass


def s24_disallow_productspec_in_ledger():
    from tests.unit._redi02_fixtures import build_small_product_graph
    from redi_lab.persistence import EvidenceLedger
    conn = _mem()
    ledger = EvidenceLedger(conn)
    g = build_small_product_graph()
    try:
        ledger.append(g["spec"])
        raise AssertionError("expected ValueError ProductSpec disallowed in master ledger")
    except ValueError as e:
        assert "ProductSpec" in str(e), str(e)


def main():
    print("REDI-02 stdlib validation runner (import-mode)")
    print(f"repo root: {REPO_ROOT}")
    print()
    checks = [
        ("s01 schema bootstrap & version & FK pragma", s01_schema_bootstrap),
        ("s02 unsupported schema version raise", s02_unsupported_schema_version),
        ("s03 ledger append all 18 event types", s03_ledger_append_event_types),
        ("s04 None/Decimal/datetime preservation", s04_ledger_none_preservation_decimal_datetime),
        ("s05 manifest freeze + amend lineage", s05_manifest_freeze_amend),
        ("s06 FactoryRun store + FK fail + close once", s06_factory_run_store_fk_close),
        ("s07 duplicate entity id explicit reject", s07_duplicate_explicit_reject),
        ("s08 No update/delete/remove public methods", s08_no_update_delete_apis),
        ("s09 No unexpected UPDATE/DELETE SQL in modules", s09_sql_update_delete_grep),
        ("s10 append_batch transaction rollback", s10_transaction_rollback),
        ("s11 global_sequence ordering monotonic +1", s11_sequence_order),
        ("s12 fixture payload hashes match for all 8 entries", s12_payload_hashes_ok_on_fixture),
        ("s13 chain verify: first OK / chain OK / tamper NO", s13_integrity_chain_and_tamper),
        ("s14 env capture excludes blocklist + defense", s14_env_capture_no_secrets),
        ("s15 ReplayResult on fixture: integrity OK + 6 events", s15_replay_on_fixture),
        ("s16 replay twice equivalent", s16_replay_double_equivalent),
        ("s17 missing experiment → IncompleteEvidenceError", s17_replay_incomplete_rejects),
        ("s18 payload tamper → integrity != OK", s18_replay_tamper_detect),
        ("s19 domain AST zero sqlite/provider + reverse-layer", s19_domain_zero_sqlite3_imports),
        ("s20 pricing evidence decimal roundtrip", s20_pricing_decimal_preserved),
        ("s21 deterministic fixture build counts", s21_fixture_build_and_counts),
        ("s22 exception class inheritance", s22_exception_inheritance),
        ("s23 EvidenceLedger rejects non-dataclass", s23_disallow_non_dataclass_in_ledger),
        ("s24 EvidenceLedger rejects ProductSpec type", s24_disallow_productspec_in_ledger),
    ]
    for name, fn in checks:
        check(name, fn)
    print()
    print("=" * 60)
    print(f"TOTAL: PASS={PASS} FAIL={FAIL} CHECKS={PASS+FAIL}")
    if FAIL:
        print("Failing checks:")
        for n, ok, why in RESULTS:
            if not ok:
                print(f"  - {n}: {why}")
    return FAIL == 0


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
