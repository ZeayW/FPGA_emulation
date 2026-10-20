"""Output ports must be connected to the right bit/net on every claimed FPGA."""

import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from emuflow.errors import ValidationError
from emuflow.split_netlist_storage import (
    DDL, SPLIT_NETLIST_APPLICATION_ID, SPLIT_NETLIST_USER_VERSION,
)
from emuflow.split_output_coverage import validate_split_output_realizations


class SplitOutputCoverageTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "split.sqlite"
        with sqlite3.connect(self.path) as connection:
            connection.executescript(DDL)
            connection.execute(f"PRAGMA application_id={SPLIT_NETLIST_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={SPLIT_NETLIST_USER_VERSION}")
            connection.execute("INSERT INTO source_nets VALUES(17,'q',NULL,1,'registered-output')")
            connection.execute("INSERT INTO pin_names VALUES(0,'out')")
            connection.execute("INSERT INTO payloads VALUES(0,'{}')")
            for part, fpga in enumerate(("source", "sink", "other-sink")):
                connection.execute("INSERT INTO parts VALUES(?,?)", (part, fpga))
                connection.execute("INSERT INTO net_segments VALUES(?,17,'original')", (part,))
                declaration = json.dumps({"id": "out", "direction": "output", "width": 2})
                connection.execute("INSERT INTO ports VALUES(?,'out',?)", (part, declaration))
                connection.execute("INSERT INTO endpoint_segments VALUES(?,17,1,0,NULL,NULL,0,1,0,0)", (part,))
        self.paths = [{"id": "original", "endpoint": {"instance": None, "port": "out", "bit": 1},
                       "path_nets": ["q"]}]
        self.registry = {"schema": "emuflow.output-realizations/v1", "realizations": [
            {"id": "copy:" + fpga, "original_path": "original", "fpga": fpga,
             "terminal_net": "q", "endpoint": {"port": "out", "bit": 1}}
            for fpga in ("source", "sink", "other-sink")
        ]}

    def check(self, registry=None, paths=None):
        return validate_split_output_realizations(
            self.registry if registry is None else registry, self.path,
            iter(self.paths if paths is None else paths),
        )

    def test_all_local_and_remote_copies_are_independently_bound(self):
        self.assertEqual(self.check(), {"status": "pass", "original_output_paths": 1,
                                       "output_realizations": 3, "terminal_output_bindings": 1})

    def test_each_original_path_requires_all_copies(self):
        for keep in ([], self.registry["realizations"][1:], self.registry["realizations"][:1]):
            with self.subTest(keep=keep), self.assertRaisesRegex(ValidationError, "actual copies"):
                self.check({**self.registry, "realizations": keep})

    def test_port_declaration_does_not_prove_connected_copy(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute("DELETE FROM endpoint_segments WHERE part_index=2")
        with self.assertRaisesRegex(ValidationError, "actual copies"):
            self.check()

    def test_wrong_net_or_bit_cannot_satisfy_binding(self):
        for field, value in (("terminal_net", "different-net"), ("endpoint", {"port": "out", "bit": 0})):
            changed = copy.deepcopy(self.registry)
            changed["realizations"][0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValidationError, "original path binding"):
                self.check(changed)

    def test_copy_must_be_a_declared_output_bit(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE ports SET record_json=? WHERE part_index=1",
                               (json.dumps({"direction": "input", "width": 2}),))
        with self.assertRaisesRegex(ValidationError, "declared output"):
            self.check()

    def test_unknown_and_duplicate_realizations_are_rejected(self):
        for change in ("unknown-path", "duplicate-owner", "duplicate-id"):
            changed = copy.deepcopy(self.registry)
            extra = copy.deepcopy(changed["realizations"][0])
            if change == "unknown-path":
                extra.update(id="unknown-copy", original_path="unknown")
            elif change == "duplicate-owner":
                extra["id"] = "duplicate-owner"
            changed["realizations"].append(extra)
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.check(changed)

    def test_two_original_paths_share_a_copy_set_without_losing_either(self):
        paths = self.paths + [{**self.paths[0], "id": "second"}]
        changed = copy.deepcopy(self.registry)
        changed["realizations"] += [{**record, "id": record["id"] + ":second", "original_path": "second"}
                                    for record in self.registry["realizations"]]
        self.assertEqual(self.check(changed, paths)["output_realizations"], 6)


if __name__ == "__main__":
    unittest.main()
