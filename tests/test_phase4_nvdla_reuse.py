"""Prepared reuse requires the original producer and uncapped publication evidence."""
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import phase4_nvdla_reuse as reuse
from emuflow.errors import ValidationError
from emuflow.prepared_inputs import recover_published_timing_report


@pytest.fixture
def publication(tmp_path):
    for name, key, table, fmt, sql in (
        ("path-database.json", "paths", "paths", "sqlite-sta-path/v1",
         "CREATE TABLE paths(clock_domain TEXT,clock_period_ns REAL); INSERT INTO paths VALUES('clk',10)"),
        ("partition-net-weights.json", "weights", "weighted_nets", "sqlite-net-weights/v1",
         "CREATE TABLE net_weights(net_id TEXT); INSERT INTO net_weights VALUES('net')"),
    ):
        payload = tmp_path / (name + ".sqlite")
        with sqlite3.connect(payload) as db:
            db.executescript("PRAGMA user_version=1; " + sql)
        reuse.write(tmp_path / name, {
            "schema": "emuflow.sta-path-database/v3" if key == "paths" else "emuflow.partition-net-weights/v2",
            "design": "fixture", "summary": {table: 1},
            "payloads": {key: {"path": payload.name, "format": fmt, "records": 1}},
        })
    (tmp_path / "opensta.log").write_text(
        "EMUFLOW_OPENSTA_DATABASE status=pass clocks=1 queried_paths=1 emitted_paths=1 output=/original/paths.tsv\n")
    return tmp_path


def test_complete_original_export_recovered_without_graph_load(publication):
    with patch("emuflow.ir.EmuIR.load", side_effect=AssertionError("graph reload")):
        result = recover_published_timing_report(publication, {"clk": 10.0}, max_paths=100)
    assert result["sta"]["path_limit_reached"] is False
    assert result["sta"]["completeness_evidence"]["original_output"] == "/original/paths.tsv"


@pytest.mark.parametrize("marker", ["",
    "EMUFLOW_OPENSTA_DATABASE status=pass clocks=1 queried_paths=100 emitted_paths=1 output=/original/paths.tsv\n",
    "EMUFLOW_OPENSTA_DATABASE status=pass clocks=1 queried_paths=2 emitted_paths=2 output=/original/paths.tsv\n",
    "EMUFLOW_OPENSTA_DATABASE status=pass clocks=2 queried_paths=1 emitted_paths=1 output=/original/paths.tsv\n",
])
def test_missing_capped_or_inconsistent_export_rejected(publication, marker):
    (publication / "opensta.log").write_text(marker)
    with pytest.raises(ValidationError):
        recover_published_timing_report(publication, {"clk": 10.0}, max_paths=100)


def test_only_exact_downstream_frame_fix_can_reuse_shared_flow():
    old = "def run_multi_fpga_flow():\n    synthesize()\n    partition()\n"
    block = "\n".join("    " + line for line in reuse._FRAME_BLOCK.splitlines()) + "\n"
    new = old.replace("    partition()", block + "    partition()")
    reuse.check_frame_only_change(old, new)
    with pytest.raises(ValueError, match="outside"):
        reuse.check_frame_only_change(old, new.replace("synthesize()", "different_synthesis()"))
    with pytest.raises(ValueError, match="outside"):
        reuse.check_frame_only_change(old, new.replace('"frame_slots": frame_slots', '"frame_slots": 32'))


def test_retargeted_payload_and_modified_metadata_rejected(tmp_path):
    source, other, metadata, link = [tmp_path / name for name in ("source", "other", "metadata", "link")]
    source.write_bytes(b"original payload")
    other.write_bytes(b"different payload")
    metadata.write_text("{}")
    link.symlink_to(source)
    files = {str(source): reuse.digest(source)}
    contract = {"schema": "emuflow.nvdla-prepared-prefix/v1", "files": files,
                "stat_identity": reuse.stat_identity(files),
                "binding_files": {str(metadata): reuse.digest(metadata)},
                "binding_symlinks": {str(link): str(source)}}
    contract = json.loads(json.dumps(contract))
    reuse.verify_reuse_binding(contract)
    link.unlink(); link.symlink_to(other)
    with pytest.raises(ValueError, match="payload binding"):
        reuse.verify_reuse_binding(contract)
    link.unlink(); link.symlink_to(source)
    metadata.write_text('{"forged": true}')
    with pytest.raises(ValueError, match="metadata"):
        reuse.verify_reuse_binding(contract)


def test_prepare_reuse_seals_old_producer_and_binds_immutable_payloads(publication, tmp_path):
    import shutil
    import phase4_nvdla_prefix as prefix
    from emuflow.default_platform import DEFAULT_PLATFORM_PATH
    from emuflow.platform import Platform
    previous = tmp_path / "previous"
    timing = previous / "common/timing"
    timing.mkdir(parents=True)
    for path in list(publication.iterdir()):
        if path.is_file():
            shutil.copy2(path, timing / path.name)
    front = previous / "common/frontend/phase1"
    front.mkdir(parents=True)
    (front / "design.emuir.sqlite").write_bytes(b"already published frontend")
    reuse.write(front / "platform.normalized.json", Platform.load(DEFAULT_PLATFORM_PATH).to_dict())
    reuse.write(front / "phase1_report.json", {"status": "pass"})
    old_root = tmp_path / "old-source"
    exporter = old_root / "scripts/opensta/export_timing_path_database.tcl"
    exporter.parent.mkdir(parents=True)
    exporter.write_text("original exporter")
    inputs = {}
    for name in ("benchmark", "architecture-timing-db", "yosys", "sta"):
        path = tmp_path / name
        path.write_text(name)
        inputs[name] = path
    argv = []
    for key, value in {**inputs, "source-root": tmp_path, "platform": DEFAULT_PLATFORM_PATH,
                       "output": previous, "native-root": tmp_path, "sta-max-paths": 100}.items():
        argv += ["--" + key, str(value)]
    reuse.write(previous / "process.json", {"status": "failed", "exit_code": -15})
    reuse.write(previous / "invocation.json", {
        "command": [sys.executable, str(old_root / "scripts/phase4_nvdla_prefix.py"), *argv], "sta_max_paths": 100})
    reuse.write(previous / "provenance.json", {
        "schema": "emuflow.nvdla-prefix-provenance/v1", "runtime": {}, "source_sha256": {},
        "files": {str(path): reuse.digest(path) for path in inputs.values()}})
    args = prefix.parser().parse_args(argv)
    args.output = tmp_path / "new-run"
    args.output.mkdir()
    args.reuse_prefix = previous
    args.reuse_producer_commit = "a" * 40
    producer = {"producer_commit": args.reuse_producer_commit, "sta_exporter_sha256": reuse.digest(exporter)}
    # Source-scope validation has its own AST test; all disk/input seals and
    # real SQLite/log recovery run here rather than stubbing the boundary.
    with patch.object(reuse, "CLOCKS", {"clk": 10.0}), \
            patch.object(reuse, "check_source_scope", return_value=producer):
        contract = reuse.prepare_reuse(args, {"runtime": {}}, prefix.parser())
    reuse.verify_reuse_binding(json.loads(json.dumps(contract)))
    assert contract["source_producer"]["producer_commit"] == "a" * 40
    report = reuse.read(Path(contract["prepared_timing"]) / "experiment-timing-report.json")
    assert report["sta"]["path_limit_reached"] is False
    assert (Path(contract["prepared_timing"]) / "path-database.json.sqlite").resolve() == timing / "path-database.json.sqlite"
    (front / "design.emuir.sqlite").write_bytes(b"changed after publication")
    with pytest.raises(ValueError, match="payload changed"):
        reuse.verify_reuse_binding(contract)


def test_downstream_changes_do_not_allow_module_level_or_upstream_changes():
    name = "src/emuflow/tdm.py"
    old = "import math\ndef reconstruct_tdm_schedule_timing():\n    return 1\ndef upstream():\n    return 2\n"
    new = old.replace("return 1", "return 3")
    reuse.check_downstream_only_change(name, old, new)
    for changed in (new + "run_side_effect()\n", new.replace("return 2", "return 4")):
        with pytest.raises(ValueError, match="outside downstream"):
            reuse.check_downstream_only_change(name, old, changed)


def test_reused_worker_does_not_request_fresh_synthesis_profiling(tmp_path):
    from types import SimpleNamespace
    import phase4_nvdla_prefix as prefix
    args = SimpleNamespace(native_root=tmp_path, output=tmp_path,
                           benchmark=tmp_path / 'benchmark.json', reuse_prefix=tmp_path)
    binding = {'prepared_frontend': str(tmp_path / 'frontend'),
               'prepared_timing': str(tmp_path / 'timing')}
    def consume(**options):
        assert options['synthesis_profile_passes'] is False
        assert not options['sources']
        assert options['prepared_frontend'] == tmp_path / 'frontend'
        raise RuntimeError('validated prepared invocation')
    with patch.object(prefix, 'read', return_value=binding), \
         patch.object(prefix, 'verify_provenance'), \
         patch.object(prefix.BenchmarkRun, 'load'), \
         patch.object(prefix, 'flow_options', return_value={'sources': ['rtl.v'],
                                                          'synthesis_profile_passes': True}), \
         patch.object(reuse, 'verify_reuse_binding'), \
         patch('emuflow.multi_fpga_flow.run_multi_fpga_flow', side_effect=consume):
        with pytest.raises(RuntimeError, match='validated prepared invocation'):
            prefix.worker(args)
