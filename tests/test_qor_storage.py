import json
import sqlite3

import pytest

from emuflow.errors import ValidationError
from emuflow.io import read_json
from emuflow.qor_storage import (
    QOR_REPORT_STORAGE_SCHEMA,
    QorReportView,
    read_qor_report,
    write_qor_report,
)
from emuflow.runtime import QOR_REPORT_SCHEMA


def _report(count=3):
    paths = []
    for index in range(count):
        paths.append({
            "path": f"path-{index:05d}",
            "representative_path": "representative",
            "path_scope": "cross-fpga",
            "clock_domain": "clk",
            "target_period_ns": 10.0,
            "physical_logic_segments_cone_bound": bool(index & 1),
            "interconnect_delay_model": "fixed-slot-v1",
            "system_delay_bound_ns": 4.0 + index,
            "target_required_time_ns": 10.0,
            "target_clock_slack_bound_ns": 6.0 - index,
            "runtime_required_time_ns": 100.0,
            "runtime_clock_slack_bound_ns": 96.0 - index,
            "extra_evidence": {"ordinal": index},
        })
    return {
        "schema": QOR_REPORT_SCHEMA,
        "status": "pass",
        "design": "design",
        "platform": "platform",
        "timing": {
            "schema": "emuflow.system-timing/v2",
            "status": "pass",
            "summary": {"timing_paths": count},
            "paths": paths,
        },
    }


def test_qor_report_uses_bounded_selector_and_lazy_paths(tmp_path):
    path = tmp_path / "qor_report.json"
    logical = _report(20000)

    owner = write_qor_report(path, logical)
    assert isinstance(owner, QorReportView)
    assert len(owner["timing"]["paths"]) == 20000
    assert owner["timing"]["paths"][1]["extra_evidence"] == {"ordinal": 1}
    owner.close()

    selector = json.loads(path.read_text())
    assert selector["schema"] == QOR_REPORT_STORAGE_SCHEMA
    assert selector["counts"] == {"timing_paths": 20000}
    assert path.stat().st_size < 4096
    assert (tmp_path / selector["artifact"]).is_file()
    assert read_json(path) == logical

    with read_qor_report(path) as view:
        paths = view["timing"]["paths"]
        assert not isinstance(paths, list)
        assert paths[-1]["path"] == "path-19999"


def test_qor_store_preserves_absent_and_explicit_null_fields(tmp_path):
    report = _report(2)
    report["timing"]["paths"][0].pop("interconnect_delay_model")
    report["timing"]["paths"][1]["interconnect_delay_model"] = None
    path = tmp_path / "qor_report.json"

    owner = write_qor_report(path, report)
    owner.close()

    expanded = read_json(path)
    assert "interconnect_delay_model" not in expanded["timing"]["paths"][0]
    assert expanded["timing"]["paths"][1]["interconnect_delay_model"] is None
    assert expanded == report


def test_qor_store_rejects_resealed_internal_corruption(tmp_path):
    path = tmp_path / "qor_report.json"
    owner = write_qor_report(path, _report())
    store = owner.store_path
    owner.close()
    selector = json.loads(path.read_text())

    connection = sqlite3.connect(store)
    connection.execute(
        "UPDATE timing_paths SET path='other' WHERE seq=1"
    )
    connection.commit()
    connection.close()
    from emuflow.io import file_sha256, write_json

    digest = file_sha256(store)
    renamed = path.with_name(f"{path.stem}.{digest}.sqlite")
    store.rename(renamed)
    selector.update(
        artifact=renamed.name,
        store_bytes=renamed.stat().st_size,
        store_sha256=digest,
    )
    write_json(path, selector, compact=True)

    with pytest.raises(ValidationError, match="order|metadata"):
        read_qor_report(path)


def test_qor_writer_rejects_duplicate_or_unsorted_paths(tmp_path):
    report = _report(2)
    report["timing"]["paths"].reverse()
    with pytest.raises(ValidationError, match="strictly ordered"):
        write_qor_report(tmp_path / "qor_report.json", report)
