import json
import sys
from pathlib import Path

from emuflow.emuir_storage import _write_emuir_sqlite_v1
from emuflow.handoff_qualification import main
from emuflow.ir import EmuIR
from emuflow.yosys import import_yosys_json

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
    previous = run("timing", old, tmp_path / "old-paths.json", *extra)
    candidate = run("timing", new, tmp_path / "new-paths.json", *extra)
    assert previous["path_records_sha256"] == candidate["path_records_sha256"]
    assert previous["records"] == candidate["records"]
    assert previous["timing"]["path_qor"] == candidate["timing"]["path_qor"]
    assert not previous["timing"]["path_limit_reached"]
