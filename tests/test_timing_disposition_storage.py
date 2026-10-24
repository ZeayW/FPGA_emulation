"""Measured-only v1 generations remain independently readable after nullable v2."""
import json
import sqlite3

import pytest

from emuflow.io import file_sha256


@pytest.mark.parametrize("kind", ["boundary", "logic"])
def test_measured_v1_storage_retains_its_exact_envelope(tmp_path, kind):
    if kind == "boundary":
        import emuflow.boundary_timing_storage as module
        from tests.test_boundary_timing_storage import _header, _record
        selector = tmp_path/"timing.json"
        with module.write_boundary_timing_storage(selector, _header(), [_record("tx-0","tx",0)]): pass
        read = module.read_boundary_timing_storage
        schema = module.BOUNDARY_TIMING_STORAGE_SCHEMA_V1
        app_id = module.BOUNDARY_TIMING_APPLICATION_ID
    else:
        import emuflow.logic_segment_timing_storage as module
        from emuflow.logic_segment_timing import import_vpr_logic_segment_timing
        from tests.test_logic_segment_timing import LogicSegmentTimingTest
        identity,_ = LogicSegmentTimingTest._typed_identity(tmp_path, count=1)
        raw = tmp_path/"raw.tsv"
        raw.write_text("endpoint\tkind\tdelay_ns\tstart_pin\tend_pin\nlogic00000000\tlaunch\t1.25\tstart00000000\tend00000000\n")
        selector = tmp_path/"timing.json"
        import_vpr_logic_segment_timing(raw,identity,selector)
        read = module.read_logic_segment_timing_storage
        schema = module.LOGIC_SEGMENT_TIMING_STORAGE_SCHEMA_V1
        app_id = module.LOGIC_SEGMENT_TIMING_APPLICATION_ID
    with read(selector) as current:
        records = list(current["endpoints" if kind == "boundary" else "segments"])
        current_store = current.store_path
    data = json.loads(selector.read_text())
    legacy = tmp_path/"legacy.sqlite"
    with sqlite3.connect(legacy) as db:
        db.executescript(module._DDL_V1)
        db.execute(f"PRAGMA application_id={app_id}")
        db.execute("PRAGMA user_version=1")
        db.execute("ATTACH DATABASE ? AS newer",(str(current_store),))
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            columns = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
            names = ",".join(columns)
            db.execute(f"INSERT INTO {table}({names}) SELECT {names} FROM newer.{table}")
        db.commit()
    digest = file_sha256(legacy)
    final = selector.parent/module._store_name(selector,digest)
    legacy.rename(final)
    data.update(schema=schema,user_version=1,artifact=final.name,store_bytes=final.stat().st_size,store_sha256=digest)
    selector.write_text(json.dumps(data))
    with read(selector) as old:
        assert list(old["endpoints" if kind == "boundary" else "segments"]) == records
    # A selector cannot relabel an old SQL generation as the nullable version.
    data["user_version"] = 2
    selector.write_text(json.dumps(data))
    from emuflow.errors import ValidationError
    with pytest.raises(ValidationError): read(selector)
