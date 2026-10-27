"""Explicit typed identity facts for reader and measurement qualification.

This helper writes isolated test inputs. Production publication, source
binding, query generation and rollback belong to the native query owner.
"""
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from emuflow.timing.logic_segment_identity_storage import read_logic_segment_identity_storage


def identity_facts(path, *, header, segments, unsupported_member_paths=()):
    def canonical(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    store = path.with_name(path.name + ".facts.sqlite")
    segment_hash, unsupported_hash = sha256(), sha256()
    columns = ("id", "kind", "system_path", "member_path", "cut_index", "fpga",
               "replace_tx_endpoint", "start_pin", "end_pin", "static_exact_segment_id",
               "start_object_kind", "end_object_kind", "cone_anchor_object_kind",
               "cone_anchor_pin")
    with sqlite3.connect(store) as db:
        db.execute("PRAGMA application_id=1162696009")
        db.execute("PRAGMA user_version=1")
        db.executescript((Path(__file__).parents[1] / "src/python/emuflow/data/schema/logic_segment_identity.sql").read_text())

        def rows():
            for seq, record in enumerate(segments):
                segment_hash.update((canonical(record) + "\n").encode())
                yield (seq, *(record.get(key) for key in columns))

        db.executemany("INSERT INTO segments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows())
        for seq, record in enumerate(unsupported_member_paths):
            unsupported_hash.update((canonical(record) + "\n").encode())
            db.execute("INSERT INTO unsupported_members VALUES (?,?,?)",
                       (seq, record["path"], record["reason"]))
        total, systems, members = db.execute(
            "SELECT count(*),count(DISTINCT system_path),count(DISTINCT member_path) FROM segments"
        ).fetchone()
        counts = {"segments": total, "system_paths": systems, "member_paths": members,
                  "unsupported_member_paths": db.execute("SELECT count(*) FROM unsupported_members").fetchone()[0]}
        header = {**header, "coverage": counts}
        source = {key: header.get(key) for key in (
            "design", "platform", "fpga", "system_routes_sha256",
            "tdm_schedule_sha256", "semantic_contract_sha256")}
        logical = sha256(canonical({"header": header, "counts": counts, "source": source,
                                   "segment_sha256": segment_hash.hexdigest(),
                                   "unsupported_sha256": unsupported_hash.hexdigest()}).encode()).hexdigest()
        db.executemany("INSERT INTO metadata VALUES (?,?)", (
            (key, canonical(value)) for key, value in (
                ("header", header), ("counts", counts), ("source", source), ("logical_sha256", logical))))
    db.close()
    digest = sha256(store.read_bytes()).hexdigest()
    target = path.with_name(f"{path.name}.store.{digest}.sqlite")
    store.rename(target)
    path.write_text(canonical({"schema": "emuflow.logic-segment-identity-storage/v1",
        "logical_schema": "emuflow.logic-segment-identity/v1", "application_id": 1162696009,
        "user_version": 1, "header": header, "counts": counts, "source": source,
        "logical_sha256": logical, "artifact": target.name,
        "store_bytes": target.stat().st_size, "store_sha256": digest}), encoding="utf-8")
    return read_logic_segment_identity_storage(path)
