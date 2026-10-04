from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from emuflow.board_link_timing import validate_board_link_timing
from emuflow.errors import ValidationError
from emuflow.platform import Platform
from emuflow.ppro_calibrated_platform import (
    generate_calibrated_platform_profiles,
    predict_transport_resources,
    validate_calibrated_platform_bundle,
    validate_transport_cost_database,
    write_calibrated_platform_profiles,
)
from emuflow.open_transport_characterization import (
    BASE_FEATURE_NAMES,
    FRAME_SLOTS,
    OPEN_TRANSPORT_MODEL,
    OPEN_TRANSPORT_PROVENANCE,
)


ROOT = Path(__file__).resolve().parents[1]


def fit_artifacts():
    capacity = {
        "schema": "emuflow.ppro-calibrated-capacity/v2",
        "excluded_observations": 0,
        "holdout_checks": [{"matches": True}],
        "all_resolved_holdouts_match": True,
        "axes": {
            axis: {
                "effective_resource_capacity": amount,
                "observation_resource": resource,
            }
            for axis, resource, amount in (
                ("lut", "lut", 3_000_000),
                ("ff", "ff", 6_000_000),
                ("bram", "bram36k", 1_500),
                ("uram", "uram288", 240),
                ("dsp", "dsp48", 2_800),
            )
        },
    }
    topology = {
        "schema": "emuflow.ppro-effective-topology-fit/v1",
        "excluded_observations": 0,
        "holdout_checks": [{"matches": True}],
        "all_holdouts_match": True,
        "directed_edges": [
            {"source": "F0", "sink": "F1", "state": "reachable", "effective_hops": 1},
            {"source": "F1", "sink": "F0", "state": "reachable", "effective_hops": 1},
        ],
    }
    payload = {
        "schema": "emuflow.ppro-payload-fit/v2",
        "excluded_observations": 0,
        "holdout_checks": [{"matches": True}],
        "all_resolved_holdouts_match": True,
        "link_signatures": [
            {
                "source": source,
                "sink": sink,
                "bidirectional": False,
                "flow_count": 1,
                "fanout": 1,
                "forced_tdm_ratio": 0,
                "ratio_one_lower_width_bits": 64,
                "ratio_one_upper_width_bits": 128,
                "observed_tdm_levels": [
                    {"width_bits": 64, "maximum_tdm_ratio": 1},
                    {"width_bits": 128, "maximum_tdm_ratio": 2},
                ],
            }
            for source, sink in (("F0", "F1"), ("F1", "F0"))
        ],
    }
    parameters = {
        name: {
            "aggressive": value * 0.9,
            "nominal": value,
            "conservative": value * 1.1,
            "identifiable": True,
        }
        for name, value in (
            ("endpoint_ns", 4.0),
            ("per_hop_ns", 2.0),
            ("contention_flow_ns", 4.0),
            ("multicast_sink_ns", 0.5),
        )
    }
    latency = {
        "schema": "emuflow.ppro-latency-fit/v2",
        "parameters": parameters,
        "tdm_ratio_delay_ns": {
            "2": {
                "aggressive": 2.7,
                "nominal": 3.0,
                "conservative": 3.3,
                "identifiable": True,
            }
        },
        "observed_tdm_ratios": [1, 2],
        "excluded_observations": 0,
        "holdout_checks": [{"relative_error": 0.05}],
        "holdout_max_relative_error": 0.05,
    }
    feature_names = list(BASE_FEATURE_NAMES) + [
        f"frame_slots_{slots}" for slots in FRAME_SLOTS[1:]
    ]
    transport = {
        "schema": "emuflow.open-transport-cost-fit/v2",
        "model": OPEN_TRANSPORT_MODEL,
        "feature_names": feature_names,
        "resources": {
            "lut": {
                "parameters": {
                    name: {
                        "aggressive": value * 0.8,
                        "nominal": value,
                        "conservative": value * 1.2,
                        "identifiable": True,
                    }
                    for name, value in zip(
                        feature_names,
                        (10.0, 1.0, 1.5, 2.0, 0.5, 2.0, 3.0, 4.0),
                    )
                }
            }
        },
        "excluded_observations": 0,
        "all_resources_identifiable": True,
        "holdout_checks": [
            {
                "resources": {
                    "lut": {
                        "actual": 10.0,
                        "predicted": 10.5,
                        "absolute_error": 0.5,
                        "relative_error": 0.05,
                    }
                }
            }
        ],
        "provenance": {
            "class": OPEN_TRANSPORT_PROVENANCE,
            "mapping_profile": "xilinx-ultrascaleplus-open-v1",
            "yosys_version": "Yosys test",
            "primitive_library_sha256": "1" * 64,
            "observation_sha256s": ["2" * 64],
        },
    }
    return capacity, topology, payload, latency, transport


class PProCalibratedPlatformTest(unittest.TestCase):
    def generate(self):
        prior = json.loads(
            (ROOT / "calibration/ppro_blackbox/priors/lx2-public-prior-v1.json").read_text(
                encoding="utf-8"
            )
        )
        capacity, topology, payload, latency, transport = fit_artifacts()
        return generate_calibrated_platform_profiles(
            prior=prior,
            configuration_id="lx2-m1",
            capacity_fit=capacity,
            topology_fit=topology,
            payload_fit=payload,
            latency_fit=latency,
            transport_fit=transport,
            fabric_clock_mhz={"aggressive": 300.0, "nominal": 250.0, "conservative": 200.0},
        )

    def test_profiles_are_valid_emuflow_databases(self):
        result = self.generate()
        self.assertEqual(set(result["profiles"]), {"aggressive", "nominal", "conservative"})
        for name, artifacts in result["profiles"].items():
            platform = Platform.from_dict(artifacts["boarddb"])
            self.assertEqual(len(platform.fpgas), 2)
            self.assertEqual(len(platform.links), 1)
            self.assertEqual(platform.links[0].data_lanes_per_direction, 64)
            for fpga in platform.fpgas:
                self.assertEqual(
                    fpga.capacity["bram"], fpga.capacity["bram18k"] // 2
                )
                self.assertEqual(fpga.capacity["dsp"], fpga.capacity["dsp48"])
                self.assertEqual(fpga.capacity["carry8"], fpga.capacity["lut"] // 8)
            timing = validate_board_link_timing(artifacts["board_link_timing"], platform)
            self.assertEqual(timing["characterized_links"], 2)
            self.assertFalse(timing["final_link_timing_signoff"])
            validate_transport_cost_database(
                artifacts["transport_cost"], expected_platform=platform.name
            )
        self.assertEqual(result["manifest"]["fabric_clock_provenance"], "research_assumption")
        provenance = result["manifest"]["parameter_provenance"]
        self.assertEqual(provenance["device.capacity"]["class"], "public_spec")
        self.assertEqual(
            provenance["link.payload_capacity"]["class"], "black_box_fitted"
        )
        self.assertEqual(
            provenance["link.capacity_sharing"],
            {
                "class": "research_assumption",
                "value": "per_direction",
                "reason": "ordinary black-box reports do not identify simultaneous reverse-direction sharing",
            },
        )
        self.assertEqual(
            provenance["transport.resource_cost"]["class"],
            OPEN_TRANSPORT_PROVENANCE,
        )
        bram_conversion = result["manifest"]["capacity_projection"][
            "demand_unit_conversions"
        ]["bram"]
        self.assertEqual(bram_conversion["observation_resource"], "bram36k")
        self.assertEqual(bram_conversion["boarddb_resource"], "bram18k")
        self.assertEqual(bram_conversion["boarddb_units_per_observation_unit"], 2.0)

    def test_transport_prediction_uses_production_structural_features(self):
        database = self.generate()["profiles"]["nominal"]["transport_cost"]
        transport = {
            "fpga": "F0",
            "frame_slots": 4,
            "source_signals": [{"index": 0, "signal": "net:tx:0"}],
            "shadow_signals": [{"index": 0, "signal": "net:rx:0"}],
            "endpoints": [
                {
                    "kind": "tx",
                    "link": "link-f0-f1",
                    "peer": "F1",
                    "lane": 0,
                    "slot": 0,
                },
                {
                    "kind": "rx",
                    "link": "link-f0-f1",
                    "peer": "F1",
                    "lane": 0,
                    "slot": 1,
                    "arrival_slot": 1,
                },
            ],
        }
        result = predict_transport_resources(database, {"F0": transport})
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["platform"], database["platform"])
        features = result["fpgas"][0]["features"]
        self.assertEqual(features["fixed_shell"], 1.0)
        self.assertEqual(features["tx_output_lanes"], 1.0)
        self.assertEqual(features["rx_shadow_bits"], 1.0)
        self.assertEqual(features["rx_arrival_slot_groups"], 1.0)
        self.assertEqual(features["frame_slots_4"], 1.0)
        self.assertGreater(result["total_resources"]["lut"], 0.0)

    def test_written_bundle_round_trips_and_detects_corruption(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "platform"
            write_calibrated_platform_profiles(root, self.generate())
            report = validate_calibrated_platform_bundle(root)
            self.assertEqual(report["status"], "pass")
            boarddb_path = root / "nominal/boarddb.json"
            boarddb = json.loads(boarddb_path.read_text(encoding="utf-8"))
            boarddb["links"][0]["latency_cycles"] += 1
            boarddb_path.write_text(json.dumps(boarddb), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "hash mismatch"):
                validate_calibrated_platform_bundle(root)

    def test_written_bundle_rejects_missing_parameter_provenance(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "platform"
            write_calibrated_platform_profiles(root, self.generate())
            manifest_path = root / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            del manifest["parameter_provenance"]["link.capacity_sharing"]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "provenance coverage"):
                validate_calibrated_platform_bundle(root)

    def test_multihop_claim_must_be_explained_by_direct_edges(self):
        prior = json.loads(
            (ROOT / "calibration/ppro_blackbox/priors/lx2-public-prior-v1.json").read_text(
                encoding="utf-8"
            )
        )
        capacity, topology, payload, latency, transport = fit_artifacts()
        topology["directed_edges"][0]["effective_hops"] = 2
        with self.assertRaisesRegex(ValidationError, "cannot explain"):
            generate_calibrated_platform_profiles(
                prior=prior,
                configuration_id="lx2-m1",
                capacity_fit=capacity,
                topology_fit=topology,
                payload_fit=payload,
                latency_fit=latency,
                transport_fit=transport,
                fabric_clock_mhz={"aggressive": 300.0, "nominal": 250.0, "conservative": 200.0},
            )

    def test_selected_configuration_requires_complete_ordered_pair_coverage(self):
        prior = json.loads(
            (ROOT / "calibration/ppro_blackbox/priors/lx2-public-prior-v1.json").read_text(
                encoding="utf-8"
            )
        )
        capacity, topology, payload, latency, transport = fit_artifacts()
        with self.assertRaisesRegex(ValidationError, "every ordered pair"):
            generate_calibrated_platform_profiles(
                prior=prior,
                configuration_id="lx2-m2",
                capacity_fit=capacity,
                topology_fit=topology,
                payload_fit=payload,
                latency_fit=latency,
                transport_fit=transport,
                fabric_clock_mhz={"aggressive": 300.0, "nominal": 250.0, "conservative": 200.0},
            )

    def test_payload_evidence_must_exactly_match_direct_edges(self):
        prior = json.loads(
            (ROOT / "calibration/ppro_blackbox/priors/lx2-public-prior-v1.json").read_text(
                encoding="utf-8"
            )
        )
        capacity, topology, payload, latency, transport = fit_artifacts()
        payload["link_signatures"].append(
            {
                **payload["link_signatures"][0],
                "source": "F0",
                "sink": "F0",
            }
        )
        with self.assertRaisesRegex(ValidationError, "payload directed-pair coverage"):
            generate_calibrated_platform_profiles(
                prior=prior,
                configuration_id="lx2-m1",
                capacity_fit=capacity,
                topology_fit=topology,
                payload_fit=payload,
                latency_fit=latency,
                transport_fit=transport,
                fabric_clock_mhz={"aggressive": 300.0, "nominal": 250.0, "conservative": 200.0},
            )

    def test_generation_rejects_failed_calibration_holdout(self):
        prior = json.loads(
            (ROOT / "calibration/ppro_blackbox/priors/lx2-public-prior-v1.json").read_text(
                encoding="utf-8"
            )
        )
        capacity, topology, payload, latency, transport = fit_artifacts()
        latency["holdout_max_relative_error"] = 0.16
        with self.assertRaisesRegex(ValidationError, "latency holdout gate"):
            generate_calibrated_platform_profiles(
                prior=prior,
                configuration_id="lx2-m1",
                capacity_fit=capacity,
                topology_fit=topology,
                payload_fit=payload,
                latency_fit=latency,
                transport_fit=transport,
                fabric_clock_mhz={"aggressive": 300.0, "nominal": 250.0, "conservative": 200.0},
            )


if __name__ == "__main__":
    unittest.main()
