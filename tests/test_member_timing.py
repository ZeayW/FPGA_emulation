"""Exact original-member observations leave compressed selection unchanged."""
import copy
import unittest

from emuflow.errors import ValidationError
from emuflow.tdm import reconstruct_tdm_schedule_timing
from emuflow.timing_routing import (
    MEMBER_CONSTRAINTS_FORMAT, compress_sta_paths, validate_member_constraints,
)
from tests.test_phase4_nvdla_timing import fixture, run as independent


def add_member_contract(values):
    originals, _assignment, _projected, routes = values[:4]
    by_id = {p["id"]: p for p in originals}
    copies = {r["id"]: r["original_path"] for r in routes["timing"]["output_realizations"]["realizations"]}
    all_ids = []
    for path in routes["timing"]["paths"]:
        ids = path["compressed_path_ids"]
        all_ids.extend(ids)
        path["member_fixed_delay_ns"] = [by_id[copies.get(member, member)]["fixed_delay_ns"] for member in ids]
    routes["timing"]["compression"] = {
        "member_constraints": MEMBER_CONSTRAINTS_FORMAT,
        "original_paths": len({copies.get(member, member) for member in all_ids}),
        "realized_paths": len(all_ids), "compressed_paths": len(routes["timing"]["paths"]),
    }


class MemberTimingTest(unittest.TestCase):
    def test_confirmed_tns_regression_and_legacy_semantics(self):
        values = fixture()
        old = reconstruct_tdm_schedule_timing(values[3], values[7], values[4])
        self.assertEqual(old["total_negative_slack_ns"], -57.)
        self.assertEqual(old["metric_semantics"], "legacy-compressed-observation/v1")
        self.assertFalse(old["original_member_tns_available"])
        add_member_contract(values)
        new = reconstruct_tdm_schedule_timing(values[3], values[7], values[4])
        self.assertEqual(new["total_negative_slack_ns"], -51.)
        self.assertEqual(new["total_negative_slack_ns"], independent(values)["total_negative_slack_ns"])
        self.assertEqual(new["metric_semantics"], "original-member-timing/v1")
        self.assertTrue(new["original_member_tns_available"])
        self.assertEqual(new["per_clock"]["core"]["timing_paths"], 2)

    def test_without_output_registry_every_member_still_counted(self):
        values = fixture()
        add_member_contract(values)
        values[3]["timing"].pop("output_realizations")
        new = reconstruct_tdm_schedule_timing(values[3], values[7], values[4])
        self.assertEqual(new["timing_paths"], 2)
        self.assertEqual(new["total_negative_slack_ns"], -51.)

    def test_remote_and_local_output_copies_count_once(self):
        values = fixture(remote_output=True)
        add_member_contract(values)
        new = reconstruct_tdm_schedule_timing(values[3], values[7], values[4])
        self.assertEqual(new["timing_paths"], 3)
        self.assertEqual(new["total_negative_slack_ns"], -72.5)
        self.assertEqual(new["per_clock"]["csb"]["timing_paths"], 1)

    def test_candidate_selection_view_is_numerically_unchanged(self):
        values = fixture(remote_output=True)
        old_candidates = []
        fields = ("worst_normalized_slack", "total_negative_normalized_slack", "negative_slack_paths",
                  "p01_normalized_slack", "median_normalized_slack")
        for ratio in [1, 2, 8]:
            values[4]["entries"][0]["tdm_ratio"] = ratio
            result = reconstruct_tdm_schedule_timing(values[3], values[7], values[4])
            old_candidates.append(tuple(result[k] for k in fields))
        add_member_contract(values)
        new_candidates = []
        for ratio in [1, 2, 8]:
            values[4]["entries"][0]["tdm_ratio"] = ratio
            result = reconstruct_tdm_schedule_timing(values[3], values[7], values[4],
                metric_semantics="legacy-compressed-selection/v1")
            self.assertFalse(result["original_member_tns_available"])
            new_candidates.append(tuple(result[k] for k in fields))
        self.assertEqual(old_candidates, new_candidates)
        self.assertEqual(sorted(range(3), key=old_candidates.__getitem__),
                         sorted(range(3), key=new_candidates.__getitem__))

    def test_compressor_preserves_sorted_members_and_nonperiod_deadline(self):
        paths = []
        for identity, fixed in [("z", 2.), ("a", 8.)]:
            paths.append(dict(id=identity, clock_domain="core", clock_period_ns=10.,
                fixed_delay_ns=fixed, slack_ns=6.-fixed, normalized_slack=(6.-fixed)/10,
                cut_signature=["a->b"], cut_nets=["n"], cut_transitions=[{"net":"n","from":"a","to":"b"}]))
        compressed = compress_sta_paths({"paths": paths})
        representative = compressed["paths"][0]
        self.assertEqual(representative["compressed_path_ids"], ["a", "z"])
        self.assertEqual(representative["member_fixed_delay_ns"], [8., 2.])
        self.assertEqual(representative["required_time_ns"], 6.)
        self.assertTrue(validate_member_constraints(compressed))

    def test_invalid_member_contracts_fail_closed(self):
        values = fixture()
        add_member_contract(values)
        good = values[3]["timing"]
        mutations = [
            lambda t: t["compression"].update(member_constraints="unknown/v1"),
            lambda t: t["compression"].pop("member_constraints"),
            lambda t: t["paths"][0].pop("member_fixed_delay_ns"),
            lambda t: t["paths"][0].update(member_fixed_delay_ns=[8., 2.]),
            lambda t: t["paths"][0].update(member_fixed_delay_ns=[2.]),
            lambda t: t["paths"][0].update(member_fixed_delay_ns=[float("nan"), 8.]),
            lambda t: t["paths"][0].update(compressed_path_ids=["hi", "hi"]),
            lambda t: t["paths"][0].update(compressed_path_ids=[{}, "hi"]),
            lambda t: t["compression"].update(realized_paths=999),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                malformed = copy.deepcopy(good)
                mutate(malformed)
                with self.assertRaises(ValidationError):
                    validate_member_constraints(malformed, routed=True)


if __name__ == "__main__":
    unittest.main()
