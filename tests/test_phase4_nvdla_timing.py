"""Original member timing differs from the compressed worst representative."""
import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from emuflow.tdm import build_tdm_schedule, reconstruct_tdm_schedule_timing
from tests.test_phase5_bank_ratio_model import platform_and_routes

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("nvdla_timing", ROOT / "scripts/phase4_nvdla_timing.py")
evaluate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluate)


def fixture(remote_output=False, include_local=False):
    platform, routes = platform_and_routes()
    originals = []
    for identity, fixed in [("lo", 2.0), ("hi", 8.0)]:
        originals.append(dict(id=identity, clock_domain="core", clock_period_ns=10.,
            fixed_delay_ns=fixed, slack_ns=10-fixed, path_nets=["n0000"],
            startpoint={"instance": "launch"}, endpoint={"instance": "capture"}))
    originals.append(dict(id="out", clock_domain="csb", clock_period_ns=10.,
        fixed_delay_ns=1., slack_ns=9., path_nets=["n0000" if remote_output else "outnet"],
        startpoint={"instance": "launch"}, endpoint={"instance": None, "port": "out", "bit": 0}))
    if include_local:
        # A local fanout of a globally cut net is not a crossing path.
        originals.append(dict(id="local", clock_domain="csb", clock_period_ns=10.,
            fixed_delay_ns=13., slack_ns=-3., path_nets=["n0000"],
            startpoint={"instance": "launch"}, endpoint={"instance": "local_capture"}))
    transition = {"net": "n0000", "from": "a", "to": "b"}
    path = routes["timing"]["paths"][0]
    path.update(path="hi", clock_domain="core", clock_period_ns=10., fixed_delay_ns=8.,
                required_time_ns=10., compressed_path_ids=["lo", "hi"])
    projected_paths = [{"id": "hi", "clock_domain": "core", "clock_period_ns": 10.,
        "fixed_delay_ns": 8., "slack_ns": 2., "cut_nets": ["n0000"],
        "cut_transitions": [transition], "compressed_path_ids": ["lo", "hi"]}]
    registry = {"schema": "emuflow.output-realizations/v1", "original_population": {
        "paths": len(originals), "path_ids_sha256": evaluate.population_digest(p["id"] for p in originals)},
        "realizations": []}
    for owner in (["a", "b"] if remote_output else ["a"]):
        local = owner == "a"
        identity = "output-copy:" + owner
        registry["realizations"].append(dict(id=identity, original_path="out", fpga=owner,
            endpoint={"port": "out", "bit": 0}, terminal_net=originals[2]["path_nets"][-1],
            clock_domain="csb", clock_period_ns=10., required_time_ns=10., fixed_delay_ns=1.,
            local=local, cut_nets=[] if local else ["n0000"], cut_transitions=[] if local else [transition]))
        if not local:
            routes["timing"]["paths"].append(dict(path=identity, clock_domain="csb", clock_period_ns=10.,
                fixed_delay_ns=1., required_time_ns=10., compressed_path_ids=[identity],
                cut_nets=["n0000"], cut_transitions=[transition]))
            projected_paths.append(dict(id=identity, clock_domain="csb", clock_period_ns=10.,
                fixed_delay_ns=1., slack_ns=9., compressed_path_ids=[identity],
                cut_nets=["n0000"], cut_transitions=[transition]))
    routes["timing"]["output_realizations"] = registry
    projected = {"paths": projected_paths, "output_realizations": copy.deepcopy(registry)}
    assignment = {"cut_nets": [{"net": "n0000", "source_fpgas": ["a"], "sink_fpgas": ["b"]}],
                  "instance_assignment": {"launch": "a", "capture": "b", "local_capture": "a"}}
    terminal = originals[2]["path_nets"][-1]
    nets = {terminal: {"drivers": [{"instance": "launch"}],
                      "sinks": [{"instance": None, "port": "out", "bit": 0}] +
                               ([{"instance": "capture"}] if remote_output else [])}}
    schedule = build_tdm_schedule(routes, platform)
    return originals, assignment, projected, routes, schedule, platform.to_dict(), nets, platform


def run(values):
    originals, assignment, projected, routes, schedule, board, nets, _platform = values
    return evaluate.summarize_population(iter(originals), assignment, projected, routes, schedule,
                                         board, original_nets=nets)


class FullOriginalTimingTest(unittest.TestCase):
    def test_exact_reproducer_replaces_representative_slack(self):
        values = fixture()
        old = reconstruct_tdm_schedule_timing(values[3], values[7], values[4])
        corrected = run(values)
        self.assertEqual(old["total_negative_slack_ns"], -57.)
        self.assertEqual(corrected["total_negative_slack_ns"], -51.)
        self.assertEqual(corrected["worst_slack_ns"], -28.5)
        self.assertEqual(corrected["original_paths"], 3)
        self.assertEqual(corrected["per_clock"]["core"]["total_negative_slack_ns"], -51.)
        self.assertEqual(corrected["per_clock"]["csb"]["worst_slack_ns"], 9.)

    def test_local_path_and_output_copies_count_each_original_once(self):
        corrected = run(fixture(remote_output=True, include_local=True))
        self.assertEqual(corrected["original_paths"], 4)
        self.assertEqual(corrected["transport_paths"], 3)
        self.assertEqual(corrected["local_paths"], 1)
        self.assertEqual(corrected["total_negative_slack_ns"], -51.-21.5-3.)
        self.assertEqual(corrected["per_clock"]["csb"]["timing_paths"], 2)
        self.assertEqual(corrected["per_clock"]["csb"]["total_negative_slack_ns"], -24.5)

    def test_actual_ratio_changes_transport_without_slot_wait_double_count(self):
        values = fixture()
        values[4]["entries"][0].update(tdm_ratio=8, slot=99, ready_slot=0)
        corrected = run(values)
        # Readiness is separately validated by main; timing uses d(R), not wait slots.
        self.assertEqual(corrected["total_negative_slack_ns"], (8-34)+(2-34))

    def test_lost_crossing_member_cannot_be_reclassified_local(self):
        values = fixture()
        values[2]["paths"][0]["compressed_path_ids"].remove("lo")
        values[3]["timing"]["paths"][0]["compressed_path_ids"].remove("lo")
        with self.assertRaisesRegex(ValueError, "cross-FPGA original member was omitted"):
            run(values)

    def test_missing_output_copy_is_detected_against_original_net_owners(self):
        values = fixture(remote_output=True)
        for registry in [values[2]["output_realizations"], values[3]["timing"]["output_realizations"]]:
            registry["realizations"] = [c for c in registry["realizations"] if c["fpga"] != "a"]
        with self.assertRaisesRegex(ValueError, "missing or extra output owner"):
            run(values)

    def test_original_population_missing_duplicate_and_foreign_member_fail(self):
        values = fixture(include_local=True)
        values[0].pop()
        with self.assertRaisesRegex(ValueError, "complete original population"):
            run(values)
        values = fixture()
        values[0].append(copy.deepcopy(values[0][0]))
        with self.assertRaisesRegex(ValueError, "duplicate original"):
            run(values)
        values = fixture()
        values[3]["timing"]["paths"][0]["compressed_path_ids"].append("foreign")
        with self.assertRaisesRegex(ValueError, "routed member population"):
            run(values)

    def test_required_time_is_original_fixed_plus_slack_not_period(self):
        values = fixture()
        for path in values[0][:2]:
            path["slack_ns"] -= 2  # 8 ns required time with a 10 ns clock.
        values[2]["paths"][0]["slack_ns"] -= 2
        values[3]["timing"]["paths"][0]["required_time_ns"] = 8.
        self.assertEqual(run(values)["total_negative_slack_ns"], -55.)

    def test_streamed_sqlite_original_population_matches_inline_evaluation(self):
        from emuflow.sta import iter_sta_path_database_paths
        values = fixture(remote_output=True, include_local=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "paths.sqlite"
            with sqlite3.connect(payload) as db:
                db.execute("CREATE TABLE paths (ordinal INTEGER PRIMARY KEY,path_id TEXT UNIQUE,clock_domain TEXT,"
                           "clock_period_ns REAL,slack_ns REAL,fixed_delay_ns REAL,start_object TEXT,start_instance TEXT,"
                           "start_port TEXT,start_bit INTEGER,end_object TEXT,end_instance TEXT,end_port TEXT,end_bit INTEGER)")
                db.execute("CREATE TABLE path_nets (path_ordinal INTEGER,net_ordinal INTEGER,net_id TEXT)")
                for ordinal, path in enumerate(values[0]):
                    start, end = path["startpoint"], path["endpoint"]
                    db.execute("INSERT INTO paths VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                        ordinal, path["id"], path["clock_domain"], path["clock_period_ns"], path["slack_ns"],
                        path["fixed_delay_ns"], "launch/Q", start.get("instance"), "Q", 0, "capture/D",
                        end.get("instance"), end.get("port", "D"), end.get("bit", 0)))
                    db.executemany("INSERT INTO path_nets VALUES (?,?,?)", [
                        (ordinal,i,net) for i,net in enumerate(path["path_nets"])])
            manifest = {"schema": "emuflow.sta-path-database/v3",
                        "normalization": {"positive_slack_scale_ns": 9., "negative_slack_scale_ns": 3., "max_clock_period_ns": 10.},
                        "payloads": {"paths": {"path": "paths.sqlite", "format": "sqlite-sta-path/v1",
                                               "records": len(values[0]), "sha256": "0"*64}}}
            result = evaluate.summarize_population(iter_sta_path_database_paths(root / "paths.json", manifest, verify_payload=False),
                values[1], values[2], values[3], values[4], values[5], original_nets=values[6])
            self.assertEqual(result, run(values))


if __name__ == "__main__":
    unittest.main()
