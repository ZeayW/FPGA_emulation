import json
import sys
import pytest
from pathlib import Path

from emuflow.ir.emuir_storage import _write_emuir_sqlite_v1
from scripts.validation.qualify_handoffs import main
from emuflow.ir.ir import EmuIR
from emuflow.frontend.yosys import import_yosys_json
from emuflow.common.errors import ValidationError

ROOT = Path(__file__).resolve().parents[1]


def test_separate_process_protocol_preserves_graph_inputs_and_timing(tmp_path, monkeypatch):
    ir = import_yosys_json(ROOT / "examples/yosys/counter.json", top="counter", clocks=["clk"])
    old, new, old_copy = [tmp_path / name for name in ("old.emuir.sqlite", "new.emuir.sqlite", "old-copy.emuir.sqlite")]
    _write_emuir_sqlite_v1(old, ir.value)

    def run(operation, source, output, *extra):
        report = tmp_path / (output.name + "-" + operation + ".report.json")
        monkeypatch.setattr(sys, "argv", ["handoff_qualification", operation, "--ir", str(source),
                            "--out", str(output), "--report", str(report), *extra])
        main()
        result = json.loads(report.read_text())
        assert result["elapsed_seconds"] >= result["operation_seconds"] >= 0
        assert result["qualification_seal_seconds"] >= 0
        assert result["peak_rss_bytes"] > 0
        return result

    converted = run("repack", old, new)
    baseline = run("repack", old, old_copy, "--storage-version", "1")
    scanned = run("scan", new, tmp_path / "unused")
    assert converted["logical_sha256"] == baseline["logical_sha256"] == scanned["logical_sha256"]
    assert converted["records"] == baseline["records"] == scanned["records"]
    assert EmuIR.load(new).value == EmuIR.load(old_copy).value == ir.value
    previous = run("export", old, tmp_path / "old-export", "--reader", "materialized")
    candidate = run("export", new, tmp_path / "new-export", "--reader", "indexed")
    assert previous["sta_inputs"] == candidate["sta_inputs"]
    for report in (previous, candidate):
        assert all(report[name] >= 0 for name in (
            "load_seconds", "model_seconds", "verilog_write_seconds", "net_map_write_seconds"))
    extra = ("--clock", "clk=10", "--sta-executable", str(ROOT / "tests/fixtures/fake_opensta_paths.py"))
    # Production STA consumes compact v2; the identified v1 artifact remains a
    # repack/export oracle rather than a second timing production route.
    with pytest.raises(ValidationError,match='compact EmuIR v2'):
        run("timing", old, tmp_path / "rejected-old-paths.json", *extra)
    repeated=tmp_path/'repeat.emuir.sqlite'
    run('repack',old,repeated)
    previous = run("timing", repeated, tmp_path / "repeat-paths.json", *extra)
    candidate = run("timing", new, tmp_path / "new-paths.json", *extra)
    assert previous["path_records_sha256"] == candidate["path_records_sha256"]
    assert previous["records"] == candidate["records"]
    assert previous["timing"]["path_qor"] == candidate["timing"]["path_qor"]
    assert not previous["timing"]["path_limit_reached"]
