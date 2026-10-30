import tempfile
import unittest
from pathlib import Path

from emuflow.common.errors import ValidationError
from emuflow.ir.ir import EmuIR
from tests.transport_fanout_reference import (
    buffer_transport_high_fanout_records,
)
from emuflow_tools.physical.xilinx_netlist import emit_xilinx_mapped_index, open_xilinx_mapped_index


def _transport_ir(fanout: int, *, cut_class: str = "combinational") -> EmuIR:
    instances = [
        {
            "id": "driver",
            "name": "driver",
            "type": "$lut",
            "resources": {"lut": 1},
            "parameters": {"LUT": "10", "WIDTH": "1"},
            "attributes": {},
            "constant_connections": [],
        }
    ]
    sinks = []
    for index in range(fanout):
        name = f"sink{index:04d}"
        instances.append(
            {
                "id": name,
                "name": name,
                "type": "$lut",
                "resources": {"lut": 1},
                "parameters": {"LUT": "10", "WIDTH": "1"},
                "attributes": {},
                "constant_connections": [],
            }
        )
        sinks.append({"instance": name, "port": "A", "bit": 0})
    return EmuIR(
        {
            "schema": "emuflow.emuir/v1",
            "design": {
                "name": "transport",
                "top": "transport",
                "source_format": "yosys-json",
            },
            "ports": [],
            "instances": instances,
            "nets": [
                {
                    "id": "wide",
                    "name": "wide",
                    "drivers": [
                        {"instance": "driver", "port": "Y", "bit": 0}
                    ],
                    "sinks": sinks,
                    "fanout": fanout,
                    "cut_class": cut_class,
                }
            ],
            "clocks": [],
            "warnings": [],
        }
    )


def _buffer_current(ir, *, max_fanout):
    # Exercise the writer-owned path used by the physical flow.
    from emuflow.ir.emuir_storage import write_emuir_sqlite
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary)/'transport.emuir.sqlite'
        reports = []
        write_emuir_sqlite(path, ir.value, _finalize=lambda connection: reports.append(
            buffer_transport_high_fanout_records(connection, max_fanout=max_fanout)))
        return reports[0], EmuIR.load(path)


class TransportFanoutTest(unittest.TestCase):
    def test_inserts_two_level_tree_and_preserves_every_sink(self) -> None:
        ir = _transport_ir(17)
        expected_sinks = list(ir.value["nets"][0]["sinks"])

        report, ir = _buffer_current(ir, max_fanout=4)

        self.assertEqual(report["summary"]["buffered_nets"], 1)
        self.assertEqual(report["summary"]["buffer_cells"], 7)
        self.assertEqual(report["summary"]["maximum_result_fanout"], 4)
        self.assertEqual(report["nets"][0]["level_buffer_counts"], [5, 2])
        self.assertLessEqual(ir.value["nets"][0]["fanout"], 4)
        leaf_sinks = sorted(
            (
                endpoint
                for net in ir.value["nets"][1:6]
                for endpoint in net["sinks"]
            ),
            key=lambda endpoint: endpoint["instance"],
        )
        self.assertEqual(leaf_sinks, expected_sinks)
        for instance in ir.value["instances"][-7:]:
            self.assertEqual(instance["type"], "$lut")
            self.assertEqual(instance["parameters"]["LUT"], "10")
            self.assertEqual(instance["resources"]["lut"], 1)

    def test_leaves_clock_and_reset_nets_for_dedicated_handling(self) -> None:
        for cut_class in ("clock", "reset"):
            ir = _transport_ir(8, cut_class=cut_class)
            report, ir = _buffer_current(ir, max_fanout=4)
            self.assertEqual(report["summary"]["buffered_nets"], 0)
            self.assertEqual(len(ir.value["instances"]), 9)
            self.assertEqual(len(ir.value["nets"]), 1)

    def test_buffer_cells_stream_to_lut1_mapped_index(self) -> None:
        ir = _transport_ir(5)
        report, ir = _buffer_current(ir, max_fanout=4)
        self.assertEqual(report["summary"]["buffer_cells"], 2)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path = root / "transport.emuir.sqlite"
            mapped_path = root / "mapped.sqlite"
            ir.save(ir_path)
            emit_xilinx_mapped_index(ir_path, mapped_path)
            with open_xilinx_mapped_index(mapped_path) as mapped:
                module = mapped["modules"]["transport"]
                buffer_names = [
                    name
                    for name in module["cells"]
                    if name.startswith("$emuflow_transport_fanout$")
                ]
                self.assertEqual(len(buffer_names), 2)
                for name in buffer_names:
                    cell = module["cells"][name]
                    self.assertEqual(cell["type"], "LUT1")
                    self.assertEqual(cell["parameters"], {"INIT": "10"})

    def test_compact_store_transform_matches_exact_two_level_facts(self):
        original = _transport_ir(17)
        report, actual = _buffer_current(original, max_fanout=4)
        expected_summary = {'buffered_nets': 1, 'buffer_cells': 7,
                            'added_nets': 7, 'maximum_original_fanout': 17,
                            'maximum_result_fanout': 4}
        self.assertEqual(report['summary'], expected_summary)
        self.assertEqual(report['nets'], [{'net': 'wide', 'net_index': 0,
            'original_fanout': 17, 'root_fanout': 2, 'levels': 2,
            'level_buffer_counts': [5, 2], 'buffer_cells': 7}])
        self.assertEqual(report['policy']['storage'], 'compact-emuir-streaming')
        cells = {row['id']: row for row in actual.value['instances']}
        nets = {row['id']: row for row in actual.value['nets']}
        stem = lambda level, group: f'$emuflow_transport_fanout$000000$l{level:02d}$g{group:06d}'
        endpoint = lambda level, group: {'instance': stem(level, group)+'$buf', 'port': 'A', 'bit': 0}
        self.assertEqual(nets['wide']['sinks'], [endpoint(1, 0), endpoint(1, 1)])
        for level, groups in ((0, 5), (1, 2)):
            for group in range(groups):
                name = stem(level, group)
                cell, net = cells[name+'$buf'], nets[name+'$net']
                self.assertEqual(cell['type'], '$lut')
                self.assertEqual(cell['constant_connections'], [])
                self.assertEqual(cell['parameters'], {'LUT': '10', 'WIDTH': '00000000000000000000000000000001'})
                self.assertEqual(cell['attributes'], {'emuflow_high_fanout_buffer': 'transport-balanced-tree-v1',
                    'emuflow_high_fanout_owner': 'wide', 'emuflow_high_fanout_level': str(level)})
                self.assertEqual(net['drivers'], [{'instance': name+'$buf', 'port': 'Y', 'bit': 0}])
                expected = (original.value['nets'][0]['sinks'][group*4:(group+1)*4] if level == 0
                            else [endpoint(0, i) for i in range(group*4, min((group+1)*4, 5))])
                self.assertEqual(net['sinks'], expected)
                self.assertEqual(net['fanout'], len(expected))
                self.assertEqual(net['fanout_buffer_owner'], 'wide')
                self.assertEqual(net['cut_class'], 'combinational')

    def test_rejects_invalid_limit(self) -> None:
        with self.assertRaisesRegex(ValidationError, ">= 2"):
            _buffer_current(_transport_ir(3), max_fanout=1)


if __name__ == "__main__":
    unittest.main()
