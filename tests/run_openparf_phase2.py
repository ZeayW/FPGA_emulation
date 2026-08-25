#!/usr/bin/env python3

import json
import sys
import tempfile
from pathlib import Path

from emuflow.architecture import ArchitectureDB
from emuflow.ir import EmuIR
from emuflow.phase1 import run_phase1
from emuflow.phase2 import run_phase2
from emuflow.placement import Placement


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: run_openparf_phase2.py REPOSITORY_ROOT")

    repository_root = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="emuflow-openparf-phase2-") as root:
        output_root = Path(root)
        phase1_root = output_root / "phase1"
        phase1 = run_phase1(
            yosys_json=repository_root / "examples/yosys/counter.json",
            platform_path=(
                repository_root
                / "platforms/virtual/xcvu3p_2fpga_p2p.json"
            ),
            output_dir=phase1_root,
            top="counter",
            clocks=["clk"],
        )
        if phase1.get("status") != "pass":
            raise RuntimeError(f"Phase 1 did not pass: {phase1}")

        ir_path = phase1_root / "design.emuir.json"
        architecture_path = (
            repository_root
            / "examples/phase2/xcvu3p_slice_fixture.arch.json"
        )
        phase2_root = output_root / "phase2"
        phase2 = run_phase2(
            ir_path=ir_path,
            architecture_path=architecture_path,
            output_dir=phase2_root,
        )
        if phase2.get("status") != "pass":
            raise RuntimeError(f"Phase 2 did not pass: {phase2}")
        if phase2.get("provider") != "openparf-root-build":
            raise RuntimeError(
                "Phase 2 did not use root-built OpenPARF: "
                f"{phase2.get('provider')!r}"
            )
        if phase2.get("placement", {}).get("status") != "legal":
            raise RuntimeError(f"Phase 2 placement is not legal: {phase2}")

        architecture = ArchitectureDB.load(architecture_path)
        ir = EmuIR.load(ir_path)
        placement = Placement.load(
            phase2_root / "placement.json", architecture, ir
        )
        if placement.summary()["cells"] != len(ir.value["instances"]):
            raise RuntimeError("Phase 2 placement cell count is incomplete")

        print(
            json.dumps(
                {
                    "status": "pass",
                    "phase1": {
                        "design": phase1["design"],
                        "instances": len(ir.value["instances"]),
                    },
                    "phase2": {
                        "provider": phase2["provider"],
                        "placement": placement.summary(),
                    },
                },
                indent=2,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
