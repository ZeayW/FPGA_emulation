"""Transport-round layers share physical bank directions and SLL wires."""

import subprocess
import tempfile
from pathlib import Path
import unittest

from emuflow.author_routing import route_system_author, validate_author_system_routes
from emuflow.errors import EmuFlowError
from emuflow.platform import Platform
from emuflow.routing import normalize_route_constraints
from tests.test_author_routing import author_router
from tests.test_phase4 import _assignment, _link, _platform_value


class AuthorBankRoundsTest(unittest.TestCase):
    def _inputs(self, banks=1, *, alternate=False, layered=True):
        bank = _link("bank", "a", "b", lanes=banks)
        bank.update(capacity_sharing="shared_bidirectional", bank_count=banks, wires_per_bank=1)
        links = [bank] + ([_link("alternate", "a", "b")] if alternate else [])
        platform = Platform.from_dict(_platform_value("bank-rounds", ["a", "b"], links))
        assignment = _assignment(platform, [("forward", "a", ["b"]), ("reverse", "b", ["a"])])
        if layered:
            assignment["cut_nets"][1]["transport_round"] = 1
        delays = {"bank": 1, **({"alternate": 10} if alternate else {})}
        constraints = normalize_route_constraints({
            "schema": "emuflow.system-route-constraints/v1", "frame_slots": 16,
            "tdm_ratio_quantum": 1, "link_delay_ns": delays,
        }, platform)
        return assignment, platform, constraints

    def _route(self, assignment, platform, constraints):
        routes = route_system_author(assignment, platform, constraints, executable=author_router())
        self.assertEqual(validate_author_system_routes(assignment, platform, routes)["status"], "pass")
        return routes

    def test_one_bank_cannot_reverse_between_transport_rounds(self):
        with self.assertRaisesRegex(EmuFlowError, "no legal author-maze route"):
            self._route(*self._inputs())

    def test_feasible_alternate_route_survives_cross_round_bank_constraint(self):
        # Before coupling, both rounds chose the cheap bank and the final
        # checker rejected the artifact, although this alternate route exists.
        routes = self._route(*self._inputs(alternate=True))
        self.assertEqual([r["tree_edges"][0]["link"] for r in routes["routes"]], ["bank", "alternate"])

    def test_two_banks_allow_one_fixed_bank_per_direction_across_rounds(self):
        routes = self._route(*self._inputs(banks=2))
        self.assertEqual([r["tree_edges"][0]["link"] for r in routes["routes"]], ["bank", "bank"])

    def test_unlayered_bank_semantics_are_preserved(self):
        with self.assertRaises(EmuFlowError):
            self._route(*self._inputs(layered=False))
        routes = self._route(*self._inputs(banks=2, layered=False))
        self.assertNotIn("round_capacity_contract", routes)
        self.assertEqual(routes["metrics"]["routed_sinks"], 2)

    def test_common_split_respects_each_layers_own_slot_budget(self):
        # Each round independently has eight signal slots, using both banks.
        # A common physical split cannot give both banks to both directions.
        text = "\n".join([
            "EMUFLOW_TLR_INPUT_V7_BANKS_V1",
            "PARAM 4 0 20 8 2 4 1 0.1 1 16 1 1 1 0 1 0 0 1",
            "ARC 0 0 0 1 0 -1 1 8 2 1 0 0 2 1",
            "ARC 1 0 1 0 1 -1 0 8 2 1 0 0 2 1",
            "ARC 2 0 2 3 2 -1 3 8 2 1 0 0 2 1",
            "ARC 3 0 3 2 3 -1 2 8 2 1 0 0 2 1",
            "DEMAND 0 0 1 8 0 0",
            "DEMAND 1 3 2 8 0 0", "",
        ])
        with tempfile.TemporaryDirectory(prefix="emuflow-bank-budget-") as temporary:
            source, output = Path(temporary) / "input", Path(temporary) / "output"
            source.write_text(text)
            result = subprocess.run([author_router(), str(source), str(output)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertFalse(output.exists())

    def _sll_inputs(self, *, shared=False, reverse=False, alternate=False):
        links = [_link("sll", "a", "b")] + ([_link("alternate", "a", "b")] if alternate else [])
        platform = Platform.from_dict(_platform_value("sll-rounds", ["a", "b"], links))
        second = ("n1", "b", ["a"]) if reverse else ("n1", "a", ["b"])
        assignment = _assignment(platform, [("n0", "a", ["b"]), second])
        assignment["cut_nets"][1]["transport_round"] = 1
        constraints = normalize_route_constraints({
            "schema": "emuflow.system-route-constraints/v1", "frame_slots": 16,
            "tdm_ratio_quantum": 1, "sll_links": ["sll"],
            "shared_capacity_links": ["sll"] if shared else [],
            "link_delay_ns": {"sll": 1, **({"alternate": 10} if alternate else {})},
        }, platform)
        return assignment, platform, constraints

    def test_non_tdm_sll_capacity_is_global_across_rounds(self):
        with self.assertRaisesRegex(EmuFlowError, "no legal author-maze route"):
            self._route(*self._sll_inputs())
        routes = self._route(*self._sll_inputs(alternate=True))
        self.assertEqual([r["tree_edges"][0]["link"] for r in routes["routes"]], ["sll", "alternate"])

    def test_sll_duplex_domains_remain_independent_when_declared_independent(self):
        routes = self._route(*self._sll_inputs(reverse=True))
        self.assertEqual(routes["metrics"]["routed_sinks"], 2)
        with self.assertRaisesRegex(EmuFlowError, "no legal author-maze route"):
            self._route(*self._sll_inputs(shared=True, reverse=True))


if __name__ == "__main__":
    unittest.main()
