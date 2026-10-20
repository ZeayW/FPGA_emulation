import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.ir import EmuIR
from emuflow.partition import (
    assign_clusters,
    build_clusters,
    normalize_partition_constraints,
    validate_cluster_assignment_balance,
)
from emuflow.platform import Platform


ROOT = Path(__file__).resolve().parents[1]


class GreedyBalanceLimitsTest(unittest.TestCase):
    def _case(self, *, bram_capacity=20, balance_dimensions=None):
        # A rare hard block shares its FPGA with ordinary LUT/FF cells.
        # BRAM count is capacity constrained, but is not balanced by default.
        ir = EmuIR({
            "schema": "emuflow.emuir/v1",
            "design": {"name": "rare_bram", "top": "rare_bram", "source_format": "test"},
            "ports": [], "clocks": [],
            "nets": [{"id": f"n{index}", "name": f"n{index}",
                      "drivers": [{"instance": f"i{index}", "port": "Q", "bit": 0}],
                      "sinks": [{"instance": f"i{sink}", "port": f"D{index}", "bit": 0}
                                for sink in range(index + 1, 40)],
                      "fanout": 39 - index, "cut_class": "register_output"}
                     for index in range(39)],
            "instances": [{"id": f"i{index}", "type": "logic",
                           "resources": {"lut": 1, "ff": 1,
                                         **({"bram": 2} if index == 0 else {})}}
                          for index in range(40)],
        })
        value = Platform.load(ROOT / "platforms/virtual/academic_vtr_4fpga_mesh.json").to_dict()
        for fpga in value["fpgas"]:
            fpga["capacity"].update(lut=100, ff=100, bram=bram_capacity)
            fpga["utilization_limit"] = 1.0
        platform = Platform.from_dict(value)
        raw = None if balance_dimensions is None else {
            "schema": "emuflow.partition-constraints/v1",
            "balance_dimensions": balance_dimensions,
        }
        constraints = normalize_partition_constraints(raw, ir, platform)
        clusters = build_clusters(ir, constraints)
        return ir, platform, constraints, clusters

    def test_unrequested_indivisible_bram_does_not_exclude_fpga_from_logic(self):
        ir, platform, constraints, clusters = self._case()
        assignment = assign_clusters(ir, platform, clusters, constraints, seed=4)
        owners = assignment["instance_assignment"]
        bram_owner = owners["i0"]
        self.assertGreater(sum(owner == bram_owner for owner in owners.values()), 1)
        counts = [sum(owner == fpga.id for owner in owners.values()) for fpga in platform.fpgas]
        self.assertTrue(all(7 <= count <= 11 for count in counts), counts)
        validate_cluster_assignment_balance(
            platform, clusters["clusters"], assignment["cluster_assignment"],
            constraints["balance_tolerance"],
            requested_balance_dimensions=constraints["balance_dimensions"],
        )

    def test_explicit_bram_balance_keeps_strict_integer_bound(self):
        ir, platform, constraints, clusters = self._case(
            balance_dimensions=["cells", "lut", "ff", "bram"])
        assignment = assign_clusters(ir, platform, clusters, constraints, seed=4)
        with self.assertRaisesRegex(ValidationError, "multi-resource balance/capacity"):
            validate_cluster_assignment_balance(
                platform, clusters["clusters"], assignment["cluster_assignment"],
                constraints["balance_tolerance"],
                requested_balance_dimensions=constraints["balance_dimensions"],
            )

    def test_unbalanced_bram_still_obeys_hard_capacity(self):
        ir, platform, constraints, clusters = self._case(bram_capacity=1)
        with self.assertRaisesRegex(ValidationError, "cannot fit any FPGA"):
            assign_clusters(ir, platform, clusters, constraints, seed=4)


if __name__ == "__main__":
    unittest.main()
