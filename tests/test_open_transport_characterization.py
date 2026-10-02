from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from emuflow.open_transport_characterization import (
    OPEN_TRANSPORT_FIT_SCHEMA,
    OPEN_TRANSPORT_MODEL,
    OPEN_TRANSPORT_OBSERVATION_SCHEMA,
    OPEN_TRANSPORT_PROVENANCE,
    _build_case,
    characterize_open_transport_case,
    fit_open_transport_cost_model,
    generate_open_transport_matrix,
    validate_open_transport_matrix,
)


class OpenTransportCharacterizationTest(unittest.TestCase):
    def observation(self, case):
        _, _, features = _build_case(case)
        vector = list(features.values())
        lut_coefficients = (10.0, 1.0, 2.0, 3.0, 4.0, 6.0, 7.0, 8.0)
        ff_coefficients = (2.0, 0.0, 1.0, 0.0, 0.0, 1.0, 2.0, 3.0)
        return {
            "schema": OPEN_TRANSPORT_OBSERVATION_SCHEMA,
            "identity": {"id": case["id"], "role": case["role"]},
            "model": OPEN_TRANSPORT_MODEL,
            "case": dict(case),
            "features": features,
            "resources": {
                "lut": round(sum(a * b for a, b in zip(vector, lut_coefficients))),
                "ff": round(sum(a * b for a, b in zip(vector, ff_coefficients))),
                "bram18k": 0,
                "dsp48": 0,
                "uram288": 0,
            },
            "mapping": {
                "mapping_profile": "xilinx-ultrascaleplus-open-v1",
                "yosys_version": "Yosys test",
                "primitive_library_sha256": "1" * 64,
                "mapped_json_sha256": "2" * 64,
                "cell_types": {},
                "unknown_cell_types": {},
                "external_macros": {},
            },
            "source": {},
            "runtime_seconds": 0.1,
            "provenance": {"class": OPEN_TRANSPORT_PROVENANCE},
        }

    def test_matrix_and_fit_have_independent_holdouts(self):
        matrix = generate_open_transport_matrix()
        self.assertEqual(validate_open_transport_matrix(matrix)["status"], "pass")
        observations = [self.observation(case) for case in matrix["cases"]]
        result = fit_open_transport_cost_model(observations, bootstrap_samples=32)
        self.assertEqual(result["schema"], OPEN_TRANSPORT_FIT_SCHEMA)
        self.assertTrue(result["all_resources_identifiable"])
        self.assertEqual(result["resources"]["bram18k"]["status"], "structural_zero")
        self.assertAlmostEqual(
            result["resources"]["lut"]["parameters"]["fixed_shell"]["nominal"],
            10.0,
            places=5,
        )
        self.assertTrue(
            all(
                resource["passed"]
                for check in result["holdout_checks"]
                for resource in check["resources"].values()
            )
        )

    def test_characterization_maps_exact_production_generators_and_cleans_raw(self):
        case = generate_open_transport_matrix()["cases"][4]

        def mapper(sources, top, output, **kwargs):
            combined = "\n".join(path.read_text(encoding="utf-8") for path in sources)
            self.assertIn("module emuflow_transport_F0", combined)
            self.assertIn("module emuflow_virtual_runtime_controller", combined)
            self.assertEqual(top, "emuflow_transport_F0")
            return {
                "status": "pass",
                "provider": "yosys-synth-xilinx",
                "family": "xcup",
                "mapping_profile": "xilinx-ultrascaleplus-open-v1",
                "primitive_audit": {
                    "status": "pass",
                    "resource_totals": {"lut": 11, "ff": 2},
                    "primitive_library_sha256": "1" * 64,
                    "mapped_json_sha256": "2" * 64,
                    "cell_types": {"LUT2": 11, "FDRE": 2},
                    "unknown_cell_types": {},
                    "external_macros": {},
                },
            }

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result = characterize_open_transport_case(
                case,
                yosys_executable="/bin/echo",
                work_root=root,
                mapper=mapper,
            )
            self.assertEqual(result["resources"]["lut"], 11)
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
