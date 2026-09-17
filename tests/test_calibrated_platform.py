import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from emuflow.calibrated_platform import (
    fit_calibrated_platform,
    materialize_calibrated_board_link_timing,
    materialize_calibrated_boarddb,
    measure_mapped_yosys_resource_units,
    validate_calibrated_partition_envelope,
    validate_calibrated_platform_application_holdout,
    validate_calibrated_platform_holdout,
)
from emuflow.errors import ValidationError
from emuflow.calibrated_platform_family import (
    load_calibrated_platform_family,
    select_calibrated_platform,
)


def template():
    return {
        "schema": "emuflow.calibrated-platform-template/v1",
        "model": {
            "name": "synthetic_calibrated_reference",
            "description": "Unit-test-only behavior model",
            "reference_alias": "synthetic-reference",
            "source_class": "synthetic_fixture",
            "authorization_id": "unit-test-public-fixture",
            "publication_scope": "public",
        },
        "device": {
            "part": "academic-calibrated-device",
            "utilization_limit": 0.75,
        },
        "link": {
            "direction": "full_duplex",
            "capacity_sharing": "per_direction",
            "fabric_clock_mhz": 250.0,
        },
        "configurations": [
            {
                "id": "2fpga-p2p",
                "fpgas": ["F0", "F1"],
                "links": [{"id": "L01", "endpoints": ["F0", "F1"]}],
            },
            {
                "id": "4fpga-ring",
                "fpgas": ["F0", "F1", "F2", "F3"],
                "links": [
                    {"id": "L01", "endpoints": ["F0", "F1"]},
                    {"id": "L12", "endpoints": ["F1", "F2"]},
                    {"id": "L23", "endpoints": ["F2", "F3"]},
                    {"id": "L30", "endpoints": ["F3", "F0"]},
                ],
            },
        ],
        "acceptance": {
            "capacity_outcome_accuracy_min": 1.0,
            "link_outcome_accuracy_min": 1.0,
            "delay_mean_relative_error_max": 0.10,
            "delay_max_relative_error_max": 0.15,
            "resource_unit_mapping_max_relative_error": 0.05,
            "application_tdm_ratio_absolute_error_max": 1,
        },
    }


def dataset(role="fit"):
    prefix = "fit" if role == "fit" else "holdout"
    result = {
        "schema": "emuflow.platform-calibration-observations/v1",
        "dataset": {
            "id": f"synthetic-{prefix}",
            "role": role,
            "reference_alias": "synthetic-reference",
            "source_class": "synthetic_fixture",
            "authorization_id": "unit-test-public-fixture",
            "publication_scope": "public",
        },
        "capacity_boundaries": [],
        "resource_unit_mappings": [],
        "link_capacity_boundaries": [],
        "link_characteristics": [],
        "link_delay_measurements": [],
    }
    if role == "fit":
        result["resource_unit_mappings"] = [
            {
                "id": "fit-lut-map-small",
                "configuration": "2fpga-p2p",
                "resource": "lut",
                "reference_units": 100,
                "academic_units": 100,
                "source_sha256": "1" * 64,
                "mapping_control": "isolated_same_rtl",
            },
            {
                "id": "fit-lut-map-large",
                "configuration": "2fpga-p2p",
                "resource": "lut",
                "reference_units": 200,
                "academic_units": 200,
                "source_sha256": "2" * 64,
                "mapping_control": "isolated_same_rtl",
            },
            {
                "id": "fit-ff-map-small",
                "configuration": "4fpga-ring",
                "resource": "ff",
                "reference_units": 100,
                "academic_units": 100,
                "source_sha256": "3" * 64,
                "mapping_control": "isolated_same_rtl",
            },
            {
                "id": "fit-ff-map-large",
                "configuration": "4fpga-ring",
                "resource": "ff",
                "reference_units": 200,
                "academic_units": 200,
                "source_sha256": "4" * 64,
                "mapping_control": "isolated_same_rtl",
            },
        ]
        result["capacity_boundaries"] = [
            {
                "id": "fit-lut-pass",
                "configuration": "2fpga-p2p",
                "resource": "lut",
                "demand_per_fpga": 740,
                "utilization_limit": 0.75,
                "outcome": "pass",
                "assignment_control": "fixed",
            },
            {
                "id": "fit-lut-fail",
                "configuration": "2fpga-p2p",
                "resource": "lut",
                "demand_per_fpga": 761,
                "utilization_limit": 0.75,
                "outcome": "capacity_fail",
                "assignment_control": "fixed",
            },
            {
                "id": "fit-ff-pass",
                "configuration": "4fpga-ring",
                "resource": "ff",
                "demand_per_fpga": 1500,
                "utilization_limit": 0.75,
                "outcome": "pass",
                "assignment_control": "fixed",
            },
            {
                "id": "fit-ff-fail",
                "configuration": "4fpga-ring",
                "resource": "ff",
                "demand_per_fpga": 1601,
                "utilization_limit": 0.75,
                "outcome": "capacity_fail",
                "assignment_control": "fixed",
            },
        ]
        result["link_capacity_boundaries"] = [
            {
                "id": "fit-link-pass",
                "configuration": "2fpga-p2p",
                "hop_count": 1,
                "offered_bits_per_cycle": 4,
                "outcome": "pass",
                "assignment_control": "fixed",
                "route_control": "fixed",
            },
            {
                "id": "fit-link-fail",
                "configuration": "2fpga-p2p",
                "hop_count": 1,
                "offered_bits_per_cycle": 5,
                "outcome": "capacity_fail",
                "assignment_control": "fixed",
                "route_control": "fixed",
            },
        ]
        result["link_characteristics"] = [
            {
                "id": "fit-link-characteristic",
                "configuration": "2fpga-p2p",
                "hop_count": 1,
                "line_rate_mbps": 8000.0,
                "phy_width_bits": 32,
                "channels_per_direction": 1,
                "max_tdm_ratio": 4,
                "base_route_delay_ns": 5.0,
                "assignment_control": "fixed",
                "route_control": "fixed",
            }
        ]
        # Ground truth: endpoint=2ns, hop=3ns, and discrete TDM-tier
        # penalties {ratio 1: 0ns, ratio 2: 4ns, ratio 4: 10ns}.
        result["link_delay_measurements"] = [
            delay("fit-delay-base", 1, 16, 0, 0, 5.0),
            delay("fit-delay-hop", 2, 16, 0, 0, 8.0),
            delay("fit-delay-payload-replicate", 1, 64, 0, 0, 5.0),
            delay("fit-delay-ratio-2", 1, 16, 1, 0, 9.0),
            delay("fit-delay-ratio-4", 1, 16, 3, 0, 15.0),
            delay("fit-delay-contention-replicate", 1, 16, 1, 4, 9.0),
        ]
    else:
        result["capacity_boundaries"] = [
            {
                "id": "holdout-lut-pass",
                "configuration": "4fpga-ring",
                "resource": "lut",
                "demand_per_fpga": 749,
                "utilization_limit": 0.75,
                "outcome": "pass",
                "assignment_control": "fixed",
            },
            {
                "id": "holdout-lut-fail",
                "configuration": "4fpga-ring",
                "resource": "lut",
                "demand_per_fpga": 751,
                "utilization_limit": 0.75,
                "outcome": "capacity_fail",
                "assignment_control": "fixed",
            },
        ]
        result["link_capacity_boundaries"] = [
            {
                "id": "holdout-link-pass",
                "configuration": "4fpga-ring",
                "hop_count": 1,
                "offered_bits_per_cycle": 4,
                "outcome": "pass",
                "assignment_control": "fixed",
                "route_control": "fixed",
            },
            {
                "id": "holdout-link-fail",
                "configuration": "4fpga-ring",
                "hop_count": 1,
                "offered_bits_per_cycle": 5,
                "outcome": "capacity_fail",
                "assignment_control": "fixed",
                "route_control": "fixed",
            },
        ]
        result["link_delay_measurements"] = [
            delay("holdout-delay", 3, 96, 2, 2, 18.0)
        ]
    return result


def delay(identifier, hops, payload, tdm_pressure, contention, observed):
    return {
        "id": identifier,
        "configuration": "4fpga-ring" if hops > 1 else "2fpga-p2p",
        "hop_count": hops,
        "payload_bits": payload,
        "max_tdm_ratio": tdm_pressure + 1,
        "contention_units": contention,
        "observed_delay_ns": observed,
        "assignment_control": "fixed",
        "route_control": "fixed",
    }


def application_holdout():
    return {
        "schema": "emuflow.calibrated-academic-platform-application-holdout/v1",
        "dataset": {
            "id": "synthetic-application-holdout",
            "role": "holdout",
            "reference_alias": "synthetic-reference",
            "source_class": "synthetic_fixture",
            "authorization_id": "unit-test-public-fixture",
            "publication_scope": "public",
        },
        "workload": {
            "id": "connected-real-rtl-fixture",
            "source_sha256": "a" * 64,
        },
        "configuration": "2fpga-p2p",
        "partition_mode": "free",
        "utilization_limit": 0.75,
        "resource_demand": {
            "reference": {"lut": 1500, "ff": 100},
            "academic": {"lut": 1500, "ff": 100},
        },
        "observed": {
            "active_fpga_count": 2,
            "max_direction_cut_bits": 3,
            "max_tdm_ratio": 4,
            "worst_cross_fpga_delay_ns": 15.0,
            "worst_path_hop_count": 1,
            "cross_fpga_path_count": 10,
        },
    }


class CalibratedPlatformTest(unittest.TestCase):
    @staticmethod
    def _write_json(path, value):
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")

    def _family_fixture(self, root: Path, *, first_admission="qualified"):
        base_model = fit_calibrated_platform(template(), dataset())
        specifications = []
        for rank, configuration in enumerate(("2fpga-p2p", "4fpga-ring"), 1):
            model = copy.deepcopy(base_model)
            model["model"]["name"] = f"synthetic_calibrated_tier_{rank}"
            model["calibration"]["fit_dataset_id"] = f"synthetic-fit-tier-{rank}"
            model_path = root / f"model-{rank}.json"
            self._write_json(model_path, model)
            model_sha = hashlib.sha256(model_path.read_bytes()).hexdigest()

            blind_observations = dataset("holdout")
            blind_observations["dataset"]["id"] = (
                f"synthetic-holdout-tier-{rank}"
            )
            for category in (
                "capacity_boundaries",
                "link_capacity_boundaries",
                "link_characteristics",
                "link_delay_measurements",
            ):
                for item in blind_observations[category]:
                    item["configuration"] = configuration
            blind = validate_calibrated_platform_holdout(
                model, blind_observations
            )
            blind_path = root / f"blind-{rank}.json"
            self._write_json(blind_path, blind)

            holdout = application_holdout()
            holdout["configuration"] = configuration
            application = validate_calibrated_platform_application_holdout(
                model, holdout
            )
            application_path = root / f"application-{rank}.json"
            self._write_json(application_path, application)
            full_flow = {
                "schema": "emuflow.calibrated-platform-full-flow-acceptance/v1",
                "status": "pass",
                "model": model["model"]["name"],
                "model_sha256": model_sha,
                "configuration": configuration,
                "profile": "nominal",
                "utilization_limit": 0.75,
                "workload": "connected-real-rtl-fixture",
                "workload_sha256": "c" * 64,
                "physical_seed": 1,
                "completed_phases": list(range(1, 8)),
                "checks": {
                    "macro_cycle_equivalence": "pass",
                    "schedule_legality": "pass",
                    "drc_violations": 0,
                    "unrouted_nets": 0,
                    "phase7c_path_coverage": 1.0,
                },
                "timing": {
                    "engine": "opensta",
                    "scope": "system_global",
                    "wns_ns": -2.5,
                    "tns_ns": -20.0,
                },
            }
            full_path = root / f"full-{rank}.json"
            self._write_json(full_path, full_flow)
            admission = first_admission if rank == 1 else "qualified"
            item = {
                "id": f"tier-{rank}",
                "service_rank": rank,
                "admission": admission,
                "model_file": model_path.name,
                "model_sha256": model_sha,
                "configuration": configuration,
                "profile": "nominal",
                "utilization_limit": 0.75,
            }
            if admission == "qualified":
                item["evidence"] = {
                    "blind_holdout": {
                        "file": blind_path.name,
                        "sha256": hashlib.sha256(blind_path.read_bytes()).hexdigest(),
                    },
                    "application_holdout": {
                        "file": application_path.name,
                        "sha256": hashlib.sha256(
                            application_path.read_bytes()
                        ).hexdigest(),
                    },
                    "full_flow_acceptance": {
                        "file": full_path.name,
                        "sha256": hashlib.sha256(full_path.read_bytes()).hexdigest(),
                    },
                }
            specifications.append(item)
        family = {
            "schema": "emuflow.calibrated-platform-family/v1",
            "family": {
                "name": "synthetic-qualified-family",
                "description": "unit test",
                "qualification": "independently_admitted_calibrated_platforms",
                "not_a_hardware_clone": True,
            },
            "selection_policy": {
                "objective": "lowest_explicit_service_tier",
                "resource_prefilter": "aggregate_effective_capacity",
                "post_partition_gate": "calibrated_partition_envelope",
            },
            "specifications": specifications,
        }
        family_path = root / "family.json"
        self._write_json(family_path, family)
        return family_path

    @staticmethod
    def _demand(lut, ff=100):
        return {
            "schema": "emuflow.calibrated-platform-design-demand/v1",
            "design": {"id": "design-under-test", "source_sha256": "d" * 64},
            "resources": {"lut": lut, "ff": ff},
        }

    def test_family_selects_smallest_qualified_capacity_fit(self):
        with tempfile.TemporaryDirectory() as directory:
            family_path = self._family_fixture(Path(directory))
            small, boarddb, timing = select_calibrated_platform(
                family_path, self._demand(1499)
            )
            self.assertEqual(small["selected_specification"], "tier-1")
            self.assertEqual(len(boarddb["fpgas"]), 2)
            self.assertEqual(len(timing["links"]), 2)
            medium, boarddb, _ = select_calibrated_platform(
                family_path, self._demand(1501)
            )
            self.assertEqual(medium["selected_specification"], "tier-2")
            self.assertEqual(len(boarddb["fpgas"]), 4)

    def test_family_binds_tier_utilization_to_evidence_and_materialization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            family_path = self._family_fixture(root)
            family = json.loads(family_path.read_text())
            family["specifications"][0]["utilization_limit"] = 0.50
            for name, evidence_key in (
                ("application-1.json", "application_holdout"),
                ("full-1.json", "full_flow_acceptance"),
            ):
                path = root / name
                value = json.loads(path.read_text())
                value["utilization_limit"] = 0.50
                self._write_json(path, value)
                family["specifications"][0]["evidence"][evidence_key][
                    "sha256"
                ] = hashlib.sha256(path.read_bytes()).hexdigest()
            self._write_json(family_path, family)

            report, boarddb, timing = select_calibrated_platform(
                family_path, self._demand(900)
            )
            self.assertEqual(report["selected_specification"], "tier-1")
            self.assertEqual(report["selected_utilization_limit"], 0.50)
            self.assertEqual(boarddb["fpgas"][0]["utilization_limit"], 0.50)
            self.assertEqual(boarddb["fpgas"][0]["effective_capacity"]["lut"], 500)
            self.assertEqual(timing["platform"], boarddb["platform"]["name"])

            report, _, _ = select_calibrated_platform(
                family_path, self._demand(1001)
            )
            self.assertEqual(report["selected_specification"], "tier-2")

    def test_family_rejects_unproven_or_overdeclared_tier_utilization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            family_path = self._family_fixture(root)
            family = json.loads(family_path.read_text())
            family["specifications"][0]["utilization_limit"] = 0.50
            self._write_json(family_path, family)
            with self.assertRaisesRegex(ValidationError, "does not pass"):
                load_calibrated_platform_family(family_path)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            family_path = self._family_fixture(root)
            family = json.loads(family_path.read_text())
            family["specifications"][0]["utilization_limit"] = 0.80
            self._write_json(family_path, family)
            with self.assertRaisesRegex(ValidationError, "may not exceed"):
                load_calibrated_platform_family(family_path)

    def test_family_excludes_candidate_and_fails_closed_when_oversized(self):
        with tempfile.TemporaryDirectory() as directory:
            family_path = self._family_fixture(
                Path(directory), first_admission="candidate"
            )
            report, boarddb, timing = select_calibrated_platform(
                family_path, self._demand(100)
            )
            self.assertEqual(report["selected_specification"], "tier-2")
            self.assertEqual(len(boarddb["fpgas"]), 4)
            oversized, boarddb, timing = select_calibrated_platform(
                family_path, self._demand(4000)
            )
            self.assertEqual(oversized["status"], "fail")
            self.assertEqual(boarddb, {})
            self.assertEqual(timing, {})

    def test_family_rejects_tampered_evidence_and_non_monotonic_tiers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            family_path = self._family_fixture(root)
            (root / "full-1.json").write_text("{}\n")
            with self.assertRaisesRegex(ValidationError, "SHA-256 mismatch"):
                load_calibrated_platform_family(family_path)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            family_path = self._family_fixture(root)
            blind_path = root / "blind-1.json"
            blind = json.loads(blind_path.read_text())
            blind["configurations"] = ["4fpga-ring"]
            self._write_json(blind_path, blind)
            family = json.loads(family_path.read_text())
            new_sha = hashlib.sha256(blind_path.read_bytes()).hexdigest()
            family["specifications"][0]["evidence"]["blind_holdout"][
                "sha256"
            ] = new_sha
            self._write_json(family_path, family)
            with self.assertRaisesRegex(ValidationError, "for the configuration"):
                load_calibrated_platform_family(family_path)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            family_path = self._family_fixture(root)
            family = json.loads(family_path.read_text())
            family["specifications"][0]["service_rank"] = 2
            family["specifications"][1]["service_rank"] = 1
            self._write_json(family_path, family)
            with self.assertRaisesRegex(ValidationError, "monotonically dominate"):
                load_calibrated_platform_family(family_path)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            family_path = self._family_fixture(root)
            family = json.loads(family_path.read_text())
            family["specifications"][1]["model_file"] = (
                family["specifications"][0]["model_file"]
            )
            family["specifications"][1]["model_sha256"] = (
                family["specifications"][0]["model_sha256"]
            )
            family["specifications"][1]["configuration"] = "2fpga-p2p"
            self._write_json(family_path, family)
            with self.assertRaisesRegex(ValidationError, "independently calibrated"):
                load_calibrated_platform_family(family_path)

    def test_resource_measurement_expands_only_reachable_hierarchy(self):
        design = {
            "modules": {
                "top": {
                    "attributes": {"top": "1"},
                    "cells": {
                        "a": {"type": "wrapper"},
                        "b": {"type": "wrapper"},
                    },
                },
                "wrapper": {
                    "attributes": {"keep_hierarchy": "yes"},
                    "cells": {
                        "l0": {"type": "LUT6"},
                        "l1": {"type": "LUT6"},
                        "l2": {"type": "LUT6"},
                        "f0": {"type": "FDRE"},
                        "f1": {"type": "FDRE"},
                        "r0": {"type": "RAMB36E2"},
                        "d0": {"type": "DSP48E2"},
                    },
                },
                "LUT6": {"attributes": {"blackbox": "1"}, "cells": {}},
                "FDRE": {"attributes": {"blackbox": "1"}, "cells": {}},
                "RAMB36E2": {"attributes": {"blackbox": "1"}, "cells": {}},
                "DSP48E2": {"attributes": {"blackbox": "1"}, "cells": {}},
                "unreachable": {
                    "attributes": {},
                    "cells": {"noise": {"type": "LUT6"}},
                },
            }
        }
        expected = {"lut": 6, "ff": 4, "bram": 2, "dsp": 2}
        for resource, units in expected.items():
            report = measure_mapped_yosys_resource_units(
                design,
                top="top",
                resource=resource,
                source_sha256="a" * 64,
            )
            self.assertEqual(report["academic_units"], units)
            self.assertEqual(report["source_sha256"], "a" * 64)

    def test_resource_measurement_groups_vtr_memory_bit_atoms(self):
        design = {
            "modules": {
                "top": {
                    "attributes": {"top": "1"},
                    "cells": {
                        "bank.mem0.bits[0].bit_cell": {
                            "type": "single_port_ram"
                        },
                        "bank.mem0.bits[1].bit_cell": {
                            "type": "single_port_ram"
                        },
                        "bank.mem1.bits[0].bit_cell": {
                            "type": "dual_port_ram"
                        },
                        "bank.mem1.bits[1].bit_cell": {
                            "type": "dual_port_ram"
                        },
                    },
                }
            }
        }
        report = measure_mapped_yosys_resource_units(
            design,
            top="top",
            resource="bram",
            source_sha256="b" * 64,
        )
        self.assertEqual(report["academic_units"], 2)
        self.assertEqual(report["mapped_resource_totals"], {"bram": 2})

    def test_fit_recovers_controlled_capacity_and_delay(self):
        model = fit_calibrated_platform(template(), dataset())
        self.assertEqual(
            model["calibration"]["reference_resource_raw_capacity_intervals"]["lut"],
            {"raw_lower": 987, "raw_upper_exclusive": 1015},
        )
        self.assertEqual(model["profiles"]["nominal"]["device_capacity"]["lut"], 1000)
        self.assertEqual(
            model["profiles"]["nominal"]["link_payload_bits_per_cycle_per_direction"],
            1,
        )
        delay_model = model["calibration"]["link_delay_model"]
        self.assertAlmostEqual(delay_model["endpoint_ns"], 2.0, places=7)
        self.assertAlmostEqual(delay_model["per_hop_ns"], 3.0, places=7)
        self.assertEqual(
            [item["ratio"] for item in delay_model["tdm_penalty_curve_ns"]],
            [1, 2, 4],
        )
        self.assertAlmostEqual(
            delay_model["tdm_penalty_curve_ns"][1]["penalty_ns"], 4.0, places=7
        )
        self.assertAlmostEqual(
            delay_model["tdm_penalty_curve_ns"][2]["penalty_ns"], 10.0, places=7
        )
        self.assertAlmostEqual(delay_model["fit_mean_relative_error"], 0.0, places=7)
        self.assertAlmostEqual(delay_model["fit_max_relative_error"], 0.0, places=7)

    def test_device_mapping_probe_may_come_from_an_auxiliary_topology(self):
        observations = dataset()
        for item in observations["resource_unit_mappings"]:
            item["configuration"] = "device-mapping-probe-topology"
        model = fit_calibrated_platform(template(), observations)
        self.assertEqual(
            model["calibration"]["resource_unit_mapping"]["lut"][
                "academic_units_per_reference_unit"
            ],
            1.0,
        )

    def test_two_fpga_platform_fits_one_hop_delay_without_fake_decomposition(self):
        one_hop_template = template()
        one_hop_template["configurations"] = [
            one_hop_template["configurations"][0]
        ]
        observations = dataset()
        for category in (
            "capacity_boundaries",
            "link_capacity_boundaries",
            "link_characteristics",
        ):
            for item in observations[category]:
                item["configuration"] = "2fpga-p2p"
        observations["link_delay_measurements"] = [
            item
            for item in observations["link_delay_measurements"]
            if item["hop_count"] == 1
        ]
        model = fit_calibrated_platform(one_hop_template, observations)
        delay_model = model["calibration"]["link_delay_model"]
        self.assertEqual(delay_model["hop_model_scope"], "declared_one_hop_only")
        self.assertEqual(delay_model["endpoint_ns"], 0.0)
        self.assertAlmostEqual(delay_model["per_hop_ns"], 5.0, places=7)

        invalid_holdout = dataset("holdout")
        for category in (
            "capacity_boundaries",
            "link_capacity_boundaries",
            "link_delay_measurements",
        ):
            for item in invalid_holdout[category]:
                item["configuration"] = "2fpga-p2p"
        with self.assertRaisesRegex(ValidationError, "one-hop-only"):
            validate_calibrated_platform_holdout(model, invalid_holdout)

        envelope = validate_calibrated_partition_envelope(
            model,
            {
                "schema": "emuflow.calibrated-platform-partition-load/v1",
                "configuration": "2fpga-p2p",
                "partition_sha256": "e" * 64,
                "max_direction_cut_bits": 1,
                "worst_path_hop_count": 2,
            },
        )
        self.assertEqual(envelope["status"], "fail")
        self.assertIn("one-hop-only", envelope["reason"])

    def test_fit_rejects_a_full_rank_but_inaccurate_delay_model(self):
        observations = dataset()
        conflicting = copy.deepcopy(observations["link_delay_measurements"][0])
        conflicting["id"] = "fit-delay-conflicting-replicate"
        conflicting["observed_delay_ns"] = 200.0
        observations["link_delay_measurements"].append(conflicting)
        with self.assertRaisesRegex(ValidationError, "does not meet acceptance"):
            fit_calibrated_platform(template(), observations)

    def test_materializes_only_declared_platform_configurations(self):
        model = fit_calibrated_platform(template(), dataset())
        boarddb = materialize_calibrated_boarddb(model, "4fpga-ring", "nominal")
        self.assertEqual(len(boarddb["fpgas"]), 4)
        self.assertEqual(len(boarddb["links"]), 4)
        self.assertEqual(boarddb["links"][0]["mode"], "abstract")
        self.assertTrue(boarddb["platform"]["name"].endswith("__nominal"))
        self.assertEqual(boarddb["fpgas"][0]["effective_capacity"]["lut"], 750)
        with self.assertRaisesRegex(ValidationError, "not an explicitly supported"):
            materialize_calibrated_boarddb(model, "8fpga-invented", "nominal")

    def test_materialization_allows_only_explicit_lower_utilization_stress(self):
        model = fit_calibrated_platform(template(), dataset())
        boarddb = materialize_calibrated_boarddb(
            model,
            "2fpga-p2p",
            "nominal",
            utilization_limit=0.10,
        )
        self.assertEqual(boarddb["fpgas"][0]["utilization_limit"], 0.10)
        self.assertTrue(boarddb["platform"]["name"].endswith("__util1000bp"))
        with self.assertRaisesRegex(ValidationError, "may not increase"):
            materialize_calibrated_boarddb(
                model,
                "2fpga-p2p",
                "nominal",
                utilization_limit=0.80,
            )

    def test_materializes_characterized_link_timing_without_cycle_rounding(self):
        model = fit_calibrated_platform(template(), dataset())
        timing = materialize_calibrated_board_link_timing(
            model, "2fpga-p2p", "nominal"
        )
        self.assertEqual(len(timing["links"]), 2)
        self.assertEqual(
            {item["qualification"] for item in timing["links"]},
            {"characterized-upper-bound"},
        )
        self.assertEqual(
            {item["delay_bound_ns"] for item in timing["links"]}, {5.0}
        )
        self.assertFalse(timing["final_link_timing_signoff"])

    def test_disjoint_holdout_passes(self):
        model = fit_calibrated_platform(template(), dataset())
        report = validate_calibrated_platform_holdout(model, dataset("holdout"))
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["configurations"], ["4fpga-ring"])
        self.assertAlmostEqual(report["gates"]["delay_max_relative_error"], 0.0)

    def test_holdout_failure_is_not_silently_accepted(self):
        model = fit_calibrated_platform(template(), dataset())
        holdout = dataset("holdout")
        holdout["link_delay_measurements"][0]["observed_delay_ns"] = 40.0
        report = validate_calibrated_platform_holdout(model, holdout)
        self.assertEqual(report["status"], "fail")

    def test_real_application_holdout_predicts_capacity_tdm_and_delay(self):
        model = fit_calibrated_platform(template(), dataset())
        report = validate_calibrated_platform_application_holdout(
            model, application_holdout()
        )
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["predicted_active_fpga_count"], 2)
        self.assertTrue(report["gates"]["resource_capacity_decisions_exact"])
        self.assertEqual(report["predicted_tdm_ratio"], 4)
        self.assertAlmostEqual(
            report["predicted_worst_cross_fpga_delay_ns"], 15.0
        )

    def test_application_holdout_gates_capacity_decisions_not_raw_mapper_counts(self):
        model = fit_calibrated_platform(template(), dataset())
        equivalent = application_holdout()
        equivalent["resource_demand"]["academic"]["lut"] = 1400
        report = validate_calibrated_platform_application_holdout(
            model, equivalent
        )
        self.assertEqual(report["status"], "pass")
        self.assertGreater(
            report["diagnostics"][
                "resource_unit_mapping_max_relative_error"
            ],
            template()["acceptance"][
                "resource_unit_mapping_max_relative_error"
            ],
        )
        self.assertTrue(report["gates"]["resource_capacity_decisions_exact"])

        inequivalent = application_holdout()
        inequivalent["resource_demand"]["academic"]["lut"] = 700
        report = validate_calibrated_platform_application_holdout(
            model, inequivalent
        )
        self.assertEqual(report["status"], "fail")
        self.assertFalse(report["gates"]["resource_capacity_decisions_exact"])

    def test_application_holdout_requires_free_partition_and_authorized_scope(self):
        model = fit_calibrated_platform(template(), dataset())
        fixed = application_holdout()
        fixed["partition_mode"] = "fixed"
        with self.assertRaisesRegex(ValidationError, "partition_mode 'free'"):
            validate_calibrated_platform_application_holdout(model, fixed)
        restricted = application_holdout()
        restricted["dataset"]["publication_scope"] = "internal"
        with self.assertRaisesRegex(ValidationError, "publication_scope"):
            validate_calibrated_platform_application_holdout(model, restricted)

    def test_fit_rejects_unmapped_resource_namespaces(self):
        observations = dataset()
        observations["resource_unit_mappings"][1]["academic_units"] = 260
        with self.assertRaisesRegex(ValidationError, "resource unit mapping"):
            fit_calibrated_platform(template(), observations)

    def test_partition_envelope_fails_before_routing(self):
        model = fit_calibrated_platform(template(), dataset())
        report = validate_calibrated_partition_envelope(
            model,
            {
                "schema": "emuflow.calibrated-platform-partition-load/v1",
                "configuration": "2fpga-p2p",
                "partition_sha256": "b" * 64,
                "max_direction_cut_bits": 5,
                "worst_path_hop_count": 1,
            },
        )
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["required_raw_tdm_ratio"], 5)
        self.assertIsNone(report["selected_characterized_tdm_ratio"])

    def test_fit_rejects_free_reference_partitioner_behavior(self):
        observations = dataset()
        observations["capacity_boundaries"][0]["assignment_control"] = "free"
        with self.assertRaisesRegex(ValidationError, "requires fixed"):
            fit_calibrated_platform(template(), observations)

    def test_fit_and_holdout_ids_must_not_overlap(self):
        model = fit_calibrated_platform(template(), dataset())
        holdout = dataset("holdout")
        holdout["link_delay_measurements"][0]["id"] = "fit-delay-base"
        with self.assertRaisesRegex(ValidationError, "overlap fit observations"):
            validate_calibrated_platform_holdout(model, holdout)

    def test_unidentifiable_delay_experiment_is_rejected(self):
        observations = dataset()
        for item in observations["link_delay_measurements"]:
            item["hop_count"] = 1
            item["max_tdm_ratio"] = 1
            item["contention_units"] = 0
        with self.assertRaisesRegex(ValidationError, "not identifiable"):
            fit_calibrated_platform(template(), observations)

    def test_authorization_identity_must_match(self):
        observations = copy.deepcopy(dataset())
        observations["dataset"]["authorization_id"] = "different-authorization"
        with self.assertRaisesRegex(ValidationError, "authorization_id"):
            fit_calibrated_platform(template(), observations)


if __name__ == "__main__":
    unittest.main()
