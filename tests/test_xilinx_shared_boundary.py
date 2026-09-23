from collections import defaultdict
from pathlib import Path
import unittest
from unittest.mock import patch

from emuflow import xilinx_segment_timing as timing
from emuflow.errors import ValidationError


def graph():
    result = timing._TimingGraph.__new__(timing._TimingGraph)
    result.edges = defaultdict(list)
    result.reverse = defaultdict(list)
    result.source_offsets = {"a": 0.25, "b": 0.25, "top:rx": 0.0}
    for source, target, delay in [
        ("a", "join", 1.0), ("b", "join", 1.0),
        ("join", "top:tx0", 2.0), ("join", "top:tx1", 3.0),
        ("top:rx", "cell:shadow/D", 0.5),
    ]:
        result._edge(source, target, delay)
    return result


class SharedBoundaryTests(unittest.TestCase):
    def test_offsets_ties_and_rx_sources(self):
        g = graph()
        arrival, origin = g._solve_longest(g.architectural_sources(), "top:tx0")
        self.assertEqual((arrival["top:tx0"], origin["top:tx0"]), (3.25, "a"))
        self.assertEqual((arrival["top:tx1"], origin["top:tx1"]), (4.25, "a"))
        self.assertEqual(g.longest(["top:rx"], "cell:shadow/D"), (0.5, "top:rx"))

    def test_cycle_and_unreachable_error_order(self):
        g = graph()
        g._edge("join", "a", 0.0)
        with self.assertRaisesRegex(ValidationError, "no path"):
            g.longest(["a"], "missing")
        with self.assertRaisesRegex(ValidationError, "combinational cycle"):
            g.longest(["a"], "top:tx0")

    def run_boundary(self, g, ports):
        identity = {"endpoints": [{
            "id": port, "kind": "rx" if port == "rx" else "tx",
            "merged_ir": {"external_port": port, "external_port_bit": 0,
                          "boundary_register_instances": ["shadow"]},
        } for port in ports]}
        with patch.object(timing, "read_json", return_value=identity), \
             patch.object(timing, "_graph", return_value=g), \
             patch.object(timing, "build_boundary_timing_database", side_effect=lambda i, m, **kw: m), \
             patch.object(timing, "validate_boundary_timing_database", return_value={}), \
             patch.object(timing, "write_json") as write:
            timing.build_xilinx_boundary_timing(*[Path("unused")] * 4)
            return write.call_args.args[1]

    def test_one_tx_solve_per_invocation_and_separate_rx(self):
        g = graph()
        with patch.object(g, "_solve_longest", wraps=g._solve_longest) as solve:
            result = self.run_boundary(g, ["tx0", "rx", "tx1"])
            self.assertEqual(solve.call_count, 2)
            self.assertEqual(result["tx1"]["delay_ns"], 4.25)
            self.assertEqual(result["rx"]["delay_ns"], 0.5)
            self.run_boundary(g, ["tx0", "tx1"])
            self.assertEqual(solve.call_count, 3)

    def test_later_unreachable_tx_is_not_hidden_by_shared_solution(self):
        g = graph()
        g._edge("unreachable", "top:tx2", 1.0)
        with self.assertRaisesRegex(ValidationError, "no path"):
            self.run_boundary(g, ["tx0", "tx2"])

    def test_ambiguous_top_port_still_rejected(self):
        g = graph()
        g._edge("a", "top:tx0[0]", 1.0)
        with self.assertRaisesRegex(ValidationError, "ambiguous"):
            self.run_boundary(g, ["tx0"])

    def test_rx_only_does_not_solve_architectural_sources(self):
        g = graph()
        with patch.object(g, "architectural_sources", side_effect=AssertionError):
            self.run_boundary(g, ["rx"])
