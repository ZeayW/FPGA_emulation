"""Mock pre-placement input for bounded contract tests, never QoR evidence.

The timing values are synthetic. The source envelope exercises the production
schema; it is not an export or qualification of the cited device data.
"""
import json
from pathlib import Path


def write_mock_xilinx_timing_contract(path: Path) -> Path:
    from emuflow.timing.xilinx_preplacement_timing import (
        XILINX_PREPLACEMENT_TIMING_SCHEMA, _REQUIRED_DELAYS,
        RAPIDWRIGHT_TIMING_DATA_REVISION, RAPIDWRIGHT_TIMING_DATA_SHA256,
    )
    path.write_text(json.dumps({
        "schema": XILINX_PREPLACEMENT_TIMING_SCHEMA,
        "family": "UltraScale+", "mapping_profile": "xilinx-ultrascaleplus-open-v1",
        "source": {
            "provider": "rapidwright-delay-model-v0.5",
            "revision": RAPIDWRIGHT_TIMING_DATA_REVISION,
            "qualification": "analytical_uncharacterized",
            "files_sha256": RAPIDWRIGHT_TIMING_DATA_SHA256,
        },
        "delays_ps": {name: 100.0 for name in sorted(_REQUIRED_DELAYS)},
        "limitations": {"authoritative_setup": "Phase 7 routed timing plus global OpenSTA"},
        "test_fixture": "synthetic delays and mocked provenance; unit contract only",
    }))
    return path
