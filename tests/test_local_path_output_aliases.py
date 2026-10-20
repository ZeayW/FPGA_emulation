import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from emuflow.errors import ValidationError
from emuflow.ir import EmuIR
from emuflow.local_path_timing import write_vpr_local_path_query
from emuflow.vtr_eblif import emit_vtr_eblif


def _endpoint(instance, port, bit=0):
    return {"instance": instance, "port": port, "bit": bit}


class LocalPathOutputAliasesTest(unittest.TestCase):
    def _fixture(self, root, *, aliases=True):
        outputs = [_endpoint(None, "y")]
        if aliases:
            outputs += [_endpoint(None, "bus", bit) for bit in (0, 1)]
        value = {"schema": "emuflow.emuir/v1", "design": {"name": "dut", "top": "dut", "source_format": "test"},
            "ports": [{"id": port, "name": port, "direction": direction, "width": width}
                      for port, direction, width in [("clk", "input", 1), ("d", "input", 1), ("y", "output", 1)]
                      + ([("bus", "output", 2)] if aliases else [])],
            "instances": [{"id": "ff", "name": "ff", "type": "FDRE", "resources": {"ff": 1},
                           "parameters": {"INIT": "0"}, "constant_connections": [
                               {"port": "CE", "bit": 0, "value": "1"}, {"port": "R", "bit": 0, "value": "0"}]}],
            "nets": [{"id": "clk", "drivers": [_endpoint(None, "clk")], "sinks": [_endpoint("ff", "C")], "cut_class": "clock"},
                     {"id": "d", "drivers": [_endpoint(None, "d")], "sinks": [_endpoint("ff", "D")], "cut_class": "primary_input"},
                     {"id": "q", "drivers": [_endpoint("ff", "Q")], "sinks": outputs, "cut_class": "register_output"}],
            "clocks": []}
        original = root / "original.emuir.sqlite"
        EmuIR(value).save(original)
        merged_value = copy.deepcopy(value)
        merged_value["design"]["name"] = "dut__fpga0"
        merged = root / "merged.emuir.sqlite"
        EmuIR(merged_value).save(merged)
        # Production partitions retain persistent cell/net IDs, which need
        # not equal the iteration ordinal used by a small materialized fixture.
        with sqlite3.connect(merged) as connection:
            connection.execute("UPDATE cell_records SET seq=7")
            connection.execute("UPDATE endpoint_records SET instance_seq=7 WHERE instance_seq IS NOT NULL")
            connection.execute("UPDATE net_records SET seq=seq+11")
            connection.execute("UPDATE endpoint_records SET net_seq=net_seq+11")
        report = emit_vtr_eblif(merged, root / "merged.eblif")
        database = {"schema": "emuflow.sta-path-database/v1", "design": "dut",
            "normalization": {"positive_slack_scale_ns": 9.0, "negative_slack_scale_ns": 1.0, "max_clock_period_ns": 10.0},
            "paths": [{"id": f"output-{index}", "startpoint": {**_endpoint("ff", "Q"), "object": "ff/Q"},
                       "endpoint": {**endpoint, "object": f"{endpoint['port']}[{endpoint['bit']}]"},
                       "clock_domain": "clk", "clock_period_ns": 10.0, "fixed_delay_ns": 1.0,
                       "slack_ns": 9.0, "normalized_slack": 1.0, "path_nets": ["q"]}
                      for index, endpoint in enumerate(outputs)]}
        assignment = {"schema": "emuflow.partition-assignment/v1", "design": "dut",
                      "instance_assignment": {"ff": "fpga0"}, "cut_nets": []}
        routes = {"schema": "emuflow.system-routes/v1", "routes": []}
        for name, document in (("database", database), ("assignment", assignment), ("routes", routes)):
            (root / (name + ".json")).write_text(json.dumps(document))
        return original, merged, report

    def _write(self, root, original, merged, **options):
        return write_vpr_local_path_query(original, root / "assignment.json", root / "database.json",
            root / "routes.json", merged, "fpga0", root / "queries.tsv", root / "identities.json", **options)

    def test_published_aliases_bind_every_output_port_and_bus_bit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original, merged, report = self._fixture(root)
            self._write(root, original, merged, eblif_report=report)
            rows = [line.split("\t") for line in (root / "queries.tsv").read_text().splitlines()[1:]]
            expected = {f"{record['packed_block']}.outpad[0]" for record in report["top_ports"] if record["direction"] == "output"}
            self.assertEqual({row[3] for row in rows}, expected)
            self.assertEqual(len(expected), 3)
            for row in rows:
                self.assertEqual(row[2], "i7.Q[0]")
                self.assertEqual(row[4].split(","), [row[2], row[3]])

    def test_missing_duplicate_or_wrong_map_does_not_drop_an_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original, merged, report = self._fixture(root)
            for mutation in (lambda r: r.pop("top_ports"),
                             lambda r: r["top_ports"].pop(),
                             lambda r: r["top_ports"].append(copy.deepcopy(r["top_ports"][-1])),
                             lambda r: r["top_ports"][-1].update(source_net="n999"),
                             lambda r: r["top_ports"][-1].update(direction="input")):
                broken = copy.deepcopy(report)
                mutation(broken)
                with self.subTest(map=broken), self.assertRaises(ValidationError):
                    self._write(root, original, merged, eblif_report=broken)
            with self.assertRaisesRegex(ValidationError, "require the eBLIF"):
                self._write(root, original, merged)

    def test_legacy_unique_output_without_report_keeps_original_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original, merged, _ = self._fixture(root, aliases=False)
            self._write(root, original, merged)
            row = (root / "queries.tsv").read_text().splitlines()[1].split("\t")
            self.assertEqual(row[2:4], ["i7.Q[0]", "out:n13.outpad[0]"])
            self.assertEqual(row[4].split(",")[-1], row[3])


if __name__ == "__main__":
    unittest.main()
