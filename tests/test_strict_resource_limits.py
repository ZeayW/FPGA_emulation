import unittest
from types import SimpleNamespace

from emuflow.errors import ValidationError
from emuflow.partition import summarize_balance_observation, validate_cluster_assignment_balance
from emuflow.platform import FpgaNode
from emuflow.partition_limits import strict_balance_limits, validate_indivisible_limits
from emuflow.resource_budget import validate_device_resource_usage, validate_materialized_resource_counts


class StrictPartitionLimitsTest(unittest.TestCase):
    def setUp(self):
        self.fpgas = [SimpleNamespace(id=f"fpga{i}", effective_capacity={"lut": 100})
                      for i in range(4)]

    def test_indivisible_cluster_rejected_without_changing_tolerance(self):
        limits = strict_balance_limits(self.fpgas, {"cells": 100, "lut": 100}, 0.1, balance_dimensions=("cells", "lut"))
        self.assertEqual(limits["fpga0"], {"cells": 27, "lut": 27})
        records = [(f"c{i}", {"cells": w, "lut": w}, None)
                   for i, w in enumerate([37, 21, 21, 21])]
        with self.assertRaisesRegex(ValidationError, "c0.*automatic relaxation is forbidden"):
            validate_indivisible_limits(records, limits)
        self.assertEqual(limits["fpga0"]["lut"], 27)

    def test_fixed_aggregate_cannot_bypass_individual_checks(self):
        limits = {f"fpga{i}": {"lut": 27} for i in range(4)}
        records = [("a", {"lut": 15}, "fpga0"), ("b", {"lut": 15}, "fpga0")]
        with self.assertRaisesRegex(ValidationError, "fixed fpga0 lut demand 30 exceeds 27"):
            validate_indivisible_limits(records, limits)

    def test_resource_integer_bound_is_not_raised_to_one(self):
        limits = strict_balance_limits(self.fpgas, {"lut": 3}, 0.1, balance_dimensions=("lut",))
        self.assertTrue(all(x["lut"] == 0 for x in limits.values()))
        with self.assertRaisesRegex(ValidationError, "indivisible cluster"):
            validate_indivisible_limits([("one", {"lut": 1}, None)], limits)

    def test_capacity_only_dimension_keeps_physical_limit(self):
        limits = strict_balance_limits(
            self.fpgas,
            {"cells": 100, "lut": 100},
            0.1,
            balance_dimensions={"cells"},
        )
        self.assertEqual(limits["fpga0"], {"cells": 27, "lut": 100})

    def test_downstream_reservation_reduces_planning_not_physical_capacity(self):
        reservations = {"fpga0": {"lut": 20}}
        balanced = strict_balance_limits(
            self.fpgas,
            {"cells": 320, "lut": 320},
            0.1,
            balance_dimensions={"cells", "lut"},
            resource_reservations_by_fpga=reservations,
        )
        # The physical-capacity share is still 25%, so the normal 88-LUT
        # balance bound applies to unreserved devices.  Only fpga0 is clipped
        # by its 80-LUT planning capacity.
        self.assertEqual(balanced["fpga0"], {"cells": 88, "lut": 80})
        self.assertEqual(balanced["fpga1"], {"cells": 88, "lut": 88})
        capacity_only = strict_balance_limits(
            self.fpgas,
            {"cells": 320, "lut": 320},
            0.1,
            balance_dimensions={"cells"},
            resource_reservations_by_fpga=reservations,
        )
        self.assertEqual(capacity_only["fpga0"]["lut"], 80)
        self.assertEqual(capacity_only["fpga1"]["lut"], 100)

    def test_validator_seals_reservation_and_rejects_original_design_overflow(self):
        clusters = [
            {
                "id": f"c{i}",
                "instances": [f"i{i}"],
                "resources": {"lut": load},
                "fixed_fpga": None,
            }
            for i, load in enumerate((81, 80, 80, 79))
        ]
        assignment = {f"c{i}": f"fpga{i}" for i in range(4)}
        reservations = {"fpga0": {"lut": 20}}
        with self.assertRaisesRegex(
            ValidationError, "multi-resource balance/capacity"
        ):
            validate_cluster_assignment_balance(
                SimpleNamespace(fpgas=self.fpgas),
                clusters,
                assignment,
                0.1,
                requested_balance_dimensions=("cells",),
                resource_reservations_by_fpga=reservations,
            )
        clusters[0]["resources"]["lut"] = 80
        clusters[3]["resources"]["lut"] = 80
        report = validate_cluster_assignment_balance(
            SimpleNamespace(fpgas=self.fpgas),
            clusters,
            assignment,
            0.1,
            requested_balance_dimensions=("cells",),
            resource_reservations_by_fpga=reservations,
        )
        self.assertEqual(report["capacity_limits"]["fpga0"]["lut"], 80)
        self.assertEqual(
            report["physical_capacity_limits"]["fpga0"]["lut"], 100
        )
        self.assertEqual(
            report["resource_reservations_by_fpga"]["fpga0"], {"lut": 20}
        )

    def test_validator_cannot_relax_for_oversized_cluster(self):
        platform = SimpleNamespace(fpgas=self.fpgas)
        clusters = [{"id": f"c{i}", "instances": list(range(w)),
                     "resources": {"lut": w}, "fixed_fpga": None}
                    for i, w in enumerate([37, 21, 21, 21])]
        with self.assertRaisesRegex(ValidationError, "multi-resource balance"):
            validate_cluster_assignment_balance(platform, clusters,
                                                {f"c{i}": f"fpga{i}" for i in range(4)}, 0.1,
                                                requested_balance_dimensions=("cells", "lut"))

    def test_balanced_assignment_reports_exact_requested_tolerance(self):
        clusters = [{"id": f"c{i}", "instances": [f"i{i}"],
                     "resources": {"lut": 25}, "fixed_fpga": None}
                    for i in range(4)]
        report = validate_cluster_assignment_balance(SimpleNamespace(fpgas=self.fpgas), clusters,
                    {f"c{i}": f"fpga{i}" for i in range(4)}, 0.1)
        self.assertEqual(report["effective_balance_percent"], 10.0)
        self.assertFalse(report["balance_auto_relaxed"])

    def test_default_accepts_40_percent_design_share_under_quarter_device_budget(self):
        fpgas = [FpgaNode(f"fpga{i}", "test", 0.25, {"lut": 400}) for i in range(4)]
        clusters = [{"id": f"c{i}", "instances": list(range(load)),
                     "resources": {"lut": load}, "fixed_fpga": None}
                    for i, load in enumerate((40, 20, 20, 20))]
        assignments = {f"c{i}": f"fpga{i}" for i in range(4)}
        report = validate_cluster_assignment_balance(
            SimpleNamespace(fpgas=fpgas), clusters, assignments, 0.1)
        self.assertEqual(report["strict_balance_dimensions"], [])
        self.assertEqual(report["balance_allowed_loads"]["fpga0"], {"cells": 100, "lut": 100})
        observed = report["balance_observation"]
        self.assertTrue(observed["diagnostic_only"])
        self.assertEqual(observed["design_share_by_fpga"]["fpga0"]["lut"], 0.4)
        self.assertEqual(observed["max_to_mean_by_dimension"]["lut"], 1.6)
        with self.assertRaisesRegex(ValidationError, "balance/capacity"):
            validate_cluster_assignment_balance(
                SimpleNamespace(fpgas=fpgas), clusters, assignments, 0.1,
                requested_balance_dimensions=("cells", "lut"))
        clusters[0]["resources"]["lut"] = 101
        with self.assertRaisesRegex(ValidationError, "balance/capacity"):
            validate_cluster_assignment_balance(
                SimpleNamespace(fpgas=fpgas), clusters, assignments, 0.1)

    def test_zero_load_balance_observation_is_defined(self):
        observed = summarize_balance_observation({"a": {"lut": 0}, "b": {"lut": 0}}, ())
        self.assertEqual(observed["max_to_mean_by_dimension"]["lut"], 0.0)
        self.assertEqual(observed["design_share_by_fpga"]["a"]["lut"], 0.0)

    def test_capacity_only_macro_can_be_imbalanced_but_not_overfilled(self):
        fpgas = [
            SimpleNamespace(
                id=f"fpga{i}",
                effective_capacity={"lut": 100, "bram": 100},
            )
            for i in range(4)
        ]
        clusters = [
            {
                "id": f"c{i}",
                "instances": [f"i{i}"],
                "resources": {"lut": 25, "bram": bram},
                "fixed_fpga": None,
            }
            for i, bram in enumerate((75, 25, 0, 0))
        ]
        assignment = {f"c{i}": f"fpga{i}" for i in range(4)}
        report = validate_cluster_assignment_balance(
            SimpleNamespace(fpgas=fpgas),
            clusters,
            assignment,
            0.1,
            requested_balance_dimensions=("cells", "lut", "ff"),
        )
        self.assertEqual(report["strict_balance_dimensions"], ["cells", "lut"])
        self.assertEqual(report["capacity_only_dimensions"], ["bram"])
        self.assertEqual(
            report["constraint_mode_by_dimension"]["bram"], "capacity"
        )
        self.assertEqual(report["balance_allowed_loads"]["fpga0"]["bram"], 100)

        clusters[0]["resources"]["bram"] = 101
        with self.assertRaisesRegex(ValidationError, "balance/capacity"):
            validate_cluster_assignment_balance(
                SimpleNamespace(fpgas=fpgas),
                clusters,
                assignment,
                0.1,
                requested_balance_dimensions=("cells", "lut", "ff"),
            )


class DeviceResourceBudgetTest(unittest.TestCase):
    def setUp(self):
        self.budget = {"id": "fpga0", "part": "test", "capacity": {"lut": 100, "ff": 200},
                       "utilization_limit": 0.25, "effective_capacity": {"lut": 25, "ff": 50}}

    def test_exact_quarter_is_legal_but_one_extra_resource_fails(self):
        result = validate_device_resource_usage({"lut": 25, "ff": 50}, self.budget,
                                                stage="post-physical-materialization")
        self.assertEqual(result["device_utilization"], {"lut": 0.25, "ff": 0.25})
        self.assertEqual(result["budget_utilization"], {"lut": 1.0, "ff": 1.0})
        with self.assertRaisesRegex(ValidationError, "lut used=26, allowed=25"):
            validate_device_resource_usage({"lut": 26, "ff": 0}, self.budget,
                                            stage="post-physical-materialization")

    def test_transport_mapping_total_is_subject_to_same_budget(self):
        with self.assertRaisesRegex(ValidationError, "post-transport-mapping"):
            validate_device_resource_usage({"lut": 24 + 2}, self.budget,
                                            stage="post-transport-mapping")

    def test_missing_physical_dimension_and_forged_capacity_are_rejected(self):
        with self.assertRaisesRegex(ValidationError, "missing resource dimensions"):
            validate_device_resource_usage({"lut": 1}, self.budget,
                                            stage="post-physical-materialization")
        with self.assertRaisesRegex(ValidationError, "effective capacity disagrees"):
            validate_device_resource_usage({"lut": 1},
                    {**self.budget, "effective_capacity": {"lut": 100, "ff": 200}},
                    stage="post-transport-mapping")


class PhysicalInventoryTest(unittest.TestCase):
    def test_transforms_count_resources_instead_of_expanded_cell_objects(self):
        mapped = {"lut": 4, "ff": 2, "dsp48": 1, "bram18k": 2}
        physical = {"lut": 3, "ff": 2, "dsp48": 1, "bram18k": 2,
                    "carry8": 0, "uram288": 0}
        transforms = {"dedicated_clock_gate_cells": 2, "fabric_prefilter_clock_gate_cells": 1}
        validate_materialized_resource_counts(mapped, physical, transforms)
        with self.assertRaisesRegex(ValidationError, "inventory disagrees"):
            validate_materialized_resource_counts(mapped, {**physical, "dsp48": 0}, transforms)
