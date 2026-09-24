"""Fail-closed capability gate for the DREAMPlaceFPGA Phase 7 candidate.

This module deliberately does not select a production placer.  It records the
capabilities of the pinned upstream DREAMPlaceFPGA Interchange implementation,
checks an EmuFlow mapped netlist against that contract, and can run a bounded
standalone Interchange probe when official ``.device`` and ``.netlist`` inputs
are already available.  The probe output is diagnostic only; it is not an
EmuFlow placement certificate.
"""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from .errors import ValidationError
from .io import read_json, write_json
from .xilinx_placer_capability import (
    XILINX_PLACER_CAPABILITY_SCHEMA,
    XILINX_PLACER_CAPABILITY_STATUSES,
    qualify_xilinx_placer_capabilities,
    validate_xilinx_placer_capability_report,
)
from .xilinx_primitives import audit_xilinx_mapped_json


DREAMPLACEFPGA_CAPABILITY_SCHEMA = XILINX_PLACER_CAPABILITY_SCHEMA
DREAMPLACEFPGA_PROBE_SCHEMA = "emuflow.dreamplacefpga-probe-result/v1"
DREAMPLACEFPGA_UPSTREAM_REVISION = (
    "004494318453ba4ad7053b12d0c924a2a2a34356"
)
DREAMPLACEFPGA_UPSTREAM_URL = (
    "https://github.com/rachelselinar/DREAMPlaceFPGA"
)

CAPABILITY_STATES = XILINX_PLACER_CAPABILITY_STATUSES


def _capability(
    state: str,
    reason: str,
    evidence: str,
    *,
    adapter_validation: str = "missing",
) -> Dict[str, Any]:
    if state not in CAPABILITY_STATES:
        raise AssertionError(f"unknown DREAMPlaceFPGA capability state {state!r}")
    return {
        "status": state,
        "evidence": [f"{evidence}: {reason}"],
        **(
            {"adapter_validation": adapter_validation}
            if state == "adapter_required"
            else {}
        ),
    }


# Evidence is pinned to the upstream revision above.  ``PlaceDB.cpp`` accepts
# only FDRE, LUTs, substring-DSP and substring-RAM movable classes.  The
# Interchange device reader creates capacity classes only for SLICE, DSP48,
# RAMBFIFO36, selected I/O, and BUFGCE sites.  Do not infer support merely
# because a primitive happens to contain "RAM" or "DSP" in its name.
_PRIMITIVE_CAPABILITIES: Dict[str, Dict[str, str]] = {
    **{
        cell_type: _capability(
            "native_supported",
            "upstream has an explicit LUT class and LUT/FF pack-legalizer mapping",
            "dreamplacefpga/ops/place_io/src/PlaceDB.cpp",
        )
        for cell_type in (
            "LUT1", "LUT2", "LUT3", "LUT4", "LUT5", "LUT6", "LUT6_2"
        )
    },
    "FDRE": _capability(
        "native_supported",
        "upstream explicitly recognizes FDRE as its FF class",
        "dreamplacefpga/ops/place_io/src/PlaceDB.cpp",
    ),
    "DSP48E2": _capability(
        "native_supported",
        "the device reader and placement database explicitly model DSP48 sites",
        "dreamplacefpga/ops/place_io/src/InterchangeDriver.cpp",
    ),
    "RAMB36E2": _capability(
        "native_supported",
        "the device reader exposes RAMBFIFO36 sites as the BRAM class",
        "dreamplacefpga/ops/place_io/src/InterchangeDriver.cpp",
    ),
    **{
        cell_type: _capability(
            "core_missing",
            "upstream recognizes only FDRE; changing FF semantics is not an adapter",
            "dreamplacefpga/ops/place_io/src/PlaceDB.cpp",
        )
        for cell_type in ("FDCE", "FDPE", "FDSE")
    },
    **{
        cell_type: _capability(
            "core_missing",
            "upstream has no exact dedicated slice-macro class or relative placement contract",
            "dreamplacefpga/ops/place_io/src/PlaceDB.cpp",
        )
        for cell_type in ("CARRY8", "MUXF7", "MUXF8", "MUXF9")
    },
    "RAMB18E2": _capability(
        "core_missing",
        "upstream exposes one RAMBFIFO36 resource class and not half-BRAM packing",
        "dreamplacefpga/ops/place_io/src/InterchangeDriver.cpp",
    ),
    "URAM288": _capability(
        "core_missing",
        "the Interchange device reader does not create a URAM site resource class",
        "dreamplacefpga/ops/place_io/src/InterchangeDriver.cpp",
    ),
    **{
        cell_type: _capability(
            "adapter_required",
            "upstream treats constants as movable LUT0 nodes; exact constant ownership must be adapted",
            "dreamplacefpga/ops/place_io/src/PlaceDB.cpp",
        )
        for cell_type in ("GND", "VCC")
    },
}


_FEATURE_CAPABILITIES: Dict[str, Dict[str, Any]] = {
    "interchange_device_input": _capability(
        "native_supported",
        "upstream directly reads compressed FPGA Interchange DeviceResources",
        "IFsupport/README.md and dreamplacefpga/ops/place_io/src/InterchangeDriver.cpp",
    ),
    "interchange_logical_netlist_input": _capability(
        "native_supported",
        "upstream directly reads FPGA Interchange LogicalNetlist",
        "IFsupport/README.md and dreamplacefpga/ops/place_io/src/InterchangeDriver.cpp",
    ),
    "emuflow_mapped_json_input": _capability(
        "adapter_required",
        "the bounded LUT/FF/DSP/BRAM36 adapter emits an official LogicalNetlist "
        "container without primitive lowering",
        "src/emuflow/dreamplacefpga_interchange.py and "
        "tests/test_dreamplacefpga_interchange.py",
        adapter_validation="pass",
    ),
    "placed_physical_netlist_output": _capability(
        "native_supported",
        "upstream can emit a placed .phys when enable_if is set",
        "dreamplacefpga/Placer.py and dreamplacefpga/IFWriter.py",
    ),
    "emuflow_placement_import": _capability(
        "adapter_required",
        "the bounded importer checks .phys cell identity, type, site/BEL "
        "legality, overlap, and full mapped-cell coverage",
        "src/emuflow/dreamplacefpga_interchange.py and "
        "tests/test_dreamplacefpga_interchange.py",
        adapter_validation="pass",
    ),
    "site_routing": _capability(
        "unverified",
        "upstream exposes enable_site_routing but the full EmuFlow primitive profile is unsupported",
        "IFsupport/README.md and dreamplacefpga/IFWriter.py",
    ),
    "multi_slr_constraints": _capability(
        "core_missing",
        "the device reader flattens sites into resource columns without an SLR constraint model",
        "dreamplacefpga/ops/place_io/src/InterchangeDriver.cpp",
    ),
    "cascade_relative_constraints": _capability(
        "core_missing",
        "no carry, DSP, BRAM, or URAM cascade-chain placement contract is exposed",
        "dreamplacefpga/ops/place_io/src/PlaceDB.cpp",
    ),
    "interchange_timing_driven_placement": _capability(
        "unverified",
        "official Interchange examples do not establish timing-driven correctness for UltraScale+",
        "IFsupport/README.md and test_interchange/*.json",
    ),
    "routing": _capability(
        "adapter_required",
        "DREAMPlaceFPGA is a placer; Route A would still use a separately validated router",
        "IFsupport/README.md",
    ),
}


_STAGE_CAPABILITIES: Dict[str, Dict[str, Any]] = {
    "global_placement": _capability(
        "native_supported",
        "upstream implements analytical global placement for Interchange inputs",
        "dreamplacefpga/NonLinearPlace.py",
    ),
    "packing": _capability(
        "native_supported",
        "upstream includes its LUT/FF packer for its recognized primitive subset",
        "dreamplacefpga/NonLinearPlace.py",
    ),
    "legalization": _capability(
        "native_supported",
        "upstream legalizes LUT/FF and its DSP/RAM resource classes",
        "dreamplacefpga/NonLinearPlace.py",
    ),
    "detailed_placement": _capability(
        "core_missing",
        "the official driver reports that detailed placement is not run",
        "dreamplacefpga/Placer.py",
    ),
    "physical_export": _capability(
        "native_supported",
        "enable_if emits a placed FPGA Interchange .phys container",
        "dreamplacefpga/Placer.py and dreamplacefpga/IFWriter.py",
    ),
}


_CONSTRAINT_CAPABILITIES: Dict[str, Dict[str, Any]] = {
    "site_bel_compatibility": _capability(
        "native_supported",
        "the pack-legalizer assigns sites and BEL indices for its supported subset",
        "dreamplacefpga/PlaceDB.py",
    ),
    "ff_control_sets": _capability(
        "native_supported",
        "upstream constructs clock/reset and enable control-set maps for FDRE",
        "dreamplacefpga/ops/place_io/src/PyPlaceDB.cpp",
    ),
    "fixed_io": _capability(
        "adapter_required",
        "official examples derive fixed I/O placement through a Vivado Tcl flow",
        "IFsupport/README.md",
    ),
    "cascade_relative_placement": _capability(
        "core_missing",
        "no dedicated carry/DSP/BRAM/URAM cascade-chain constraint is exposed",
        "dreamplacefpga/ops/place_io/src/PlaceDB.cpp",
    ),
    "clock_regions": _capability(
        "core_missing",
        "the Interchange reader does not preserve clock-region legality constraints",
        "dreamplacefpga/ops/place_io/src/InterchangeDriver.cpp",
    ),
    "multi_slr_regions": _capability(
        "core_missing",
        "the Interchange reader flattens the device and exposes no SLR constraint model",
        "dreamplacefpga/ops/place_io/src/InterchangeDriver.cpp",
    ),
}


def _select_top(source: Mapping[str, Any], top: Optional[str]) -> tuple[str, Mapping[str, Any]]:
    modules = source.get("modules")
    if not isinstance(modules, dict) or not modules:
        raise ValidationError("mapped Yosys JSON contains no modules")
    if top is not None:
        module = modules.get(top)
        if not isinstance(module, dict):
            raise ValidationError(f"mapped Yosys JSON is missing top {top!r}")
        return top, module
    if len(modules) == 1:
        name, module = next(iter(modules.items()))
        return name, module
    marked = [
        name
        for name, module in modules.items()
        if isinstance(module, dict)
        and str(module.get("attributes", {}).get("top", "0")) not in {"", "0"}
    ]
    if len(marked) != 1:
        raise ValidationError("mapped Yosys JSON top is ambiguous")
    return marked[0], modules[marked[0]]


def _probe_python_modules(python: Path) -> Dict[str, bool]:
    script = (
        "import importlib.util,json;"
        "print(json.dumps({n:bool(importlib.util.find_spec(n)) "
        "for n in ('capnp','torch','dreamplacefpga')},sort_keys=True))"
    )
    try:
        completed = subprocess.run(
            [str(python), "-c", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"capnp": False, "torch": False, "dreamplacefpga": False}
    if completed.returncode != 0:
        return {"capnp": False, "torch": False, "dreamplacefpga": False}
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"capnp": False, "torch": False, "dreamplacefpga": False}
    return {name: value.get(name) is True for name in ("capnp", "torch", "dreamplacefpga")}


def probe_dreamplacefpga_runtime(
    root: Optional[Path], *, python: Path = Path(sys.executable)
) -> Dict[str, Any]:
    """Inspect a candidate installation without importing it into EmuFlow."""

    modules = _probe_python_modules(python)
    files = {}
    if root is not None:
        root = root.resolve()
        for name, relative in (
            ("placer", "dreamplacefpga/Placer.py"),
            ("if_writer", "dreamplacefpga/IFWriter.py"),
            ("configuration", "dreamplacefpga/configure.py"),
        ):
            files[name] = (root / relative).is_file()
        place_io = list((root / "dreamplacefpga/ops/place_io").glob("place_io*.so"))
        files["compiled_place_io"] = bool(place_io)
    else:
        files = {
            "placer": False,
            "if_writer": False,
            "configuration": False,
            "compiled_place_io": False,
        }
    missing = sorted(
        [name for name, present in modules.items() if not present]
        + [name for name, present in files.items() if not present]
    )
    return {
        "state": "native_supported" if not missing else "core_missing",
        "python": str(python),
        "root": str(root) if root is not None else None,
        "python_modules": modules,
        "files": files,
        "missing": missing,
    }


def assess_dreamplacefpga_candidate(
    mapped_json: Path,
    *,
    top: Optional[str] = None,
    dreamplace_root: Optional[Path] = None,
    python: Path = Path(sys.executable),
    interchange_device: Optional[Path] = None,
    interchange_netlist: Optional[Path] = None,
    report_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Classify a mapped design against the pinned upstream capability set."""

    audit = audit_xilinx_mapped_json(mapped_json, top=top)
    source = read_json(mapped_json)
    selected_top, module = _select_top(source, top)
    cells = module.get("cells")
    if not isinstance(cells, dict):
        raise ValidationError("mapped Yosys JSON cells are invalid")
    inventory = Counter(cell.get("type") for cell in cells.values())
    primitive_capabilities: Dict[str, Any] = {}
    blockers = []
    for cell_type, count in sorted(inventory.items(), key=lambda item: str(item[0])):
        capability = _PRIMITIVE_CAPABILITIES.get(str(cell_type))
        if capability is None:
            capability = _capability(
                "unverified",
                "the pinned upstream audit has no evidence for this primitive",
                "pinned DREAMPlaceFPGA source audit",
            )
        primitive_capabilities[str(cell_type)] = {**capability, "instances": count}

    runtime = probe_dreamplacefpga_runtime(dreamplace_root, python=python)
    if runtime["state"] != "native_supported":
        blockers.append("runtime:core_missing")
    inputs = {
        "device": {
            "path": str(interchange_device) if interchange_device else None,
            "state": (
                "native_supported"
                if interchange_device is not None and interchange_device.is_file()
                else "adapter_required"
            ),
        },
        "logical_netlist": {
            "path": str(interchange_netlist) if interchange_netlist else None,
            "state": (
                "native_supported"
                if interchange_netlist is not None and interchange_netlist.is_file()
                else "adapter_required"
            ),
        },
    }
    for name, value in inputs.items():
        if value["state"] != "native_supported":
            blockers.append(f"input:{name}:adapter_required")

    common_report = {
        "schema": DREAMPLACEFPGA_CAPABILITY_SCHEMA,
        "provider": "dreamplacefpga-interchange-candidate",
        "revision": DREAMPLACEFPGA_UPSTREAM_REVISION,
        "stages": _STAGE_CAPABILITIES,
        "primitives": _PRIMITIVE_CAPABILITIES,
        "constraints": _CONSTRAINT_CAPABILITIES,
    }
    checked_common = validate_xilinx_placer_capability_report(common_report)
    qualification = qualify_xilinx_placer_capabilities(
        checked_common,
        required_primitives=(str(cell_type) for cell_type in inventory),
        required_constraints=_CONSTRAINT_CAPABILITIES,
    )
    blockers.extend(qualification["missing_entries"])
    blockers.extend(qualification["blocked_entries"])

    result = {
        **common_report,
        "status": "pass",
        "upstream_url": DREAMPLACEFPGA_UPSTREAM_URL,
        "top": selected_top,
        "mapped_audit": audit,
        "observed_primitives": primitive_capabilities,
        "feature_capabilities": dict(sorted(_FEATURE_CAPABILITIES.items())),
        "runtime": runtime,
        "inputs": inputs,
        "qualification": qualification,
        "execution_ready": not blockers,
        "blockers": sorted(set(blockers)),
        "qualification_boundary": (
            "standalone diagnostic only; not an EmuFlow placement certificate"
        ),
    }
    if report_path is not None:
        write_json(report_path, result, compact=True)
    return result


def require_dreamplacefpga_execution_ready(report: Mapping[str, Any]) -> None:
    validate_xilinx_placer_capability_report(report)
    if report.get("execution_ready") is not True:
        blockers = report.get("blockers")
        detail = ", ".join(blockers) if isinstance(blockers, list) else "unknown"
        raise ValidationError(f"DREAMPlaceFPGA candidate is not execution-ready: {detail}")


def _validate_phys_container(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValidationError("DREAMPlaceFPGA did not produce a non-empty .phys file")
    try:
        with gzip.open(path, "rb") as stream:
            prefix = stream.read(16)
    except (OSError, EOFError) as error:
        raise ValidationError("DREAMPlaceFPGA .phys output is not valid gzip") from error
    if not prefix:
        raise ValidationError("DREAMPlaceFPGA .phys payload is empty")


def run_dreamplacefpga_interchange_probe(
    report: Mapping[str, Any],
    output_dir: Path,
    *,
    python: Path = Path(sys.executable),
    extra_args: Sequence[str] = (),
) -> Dict[str, Any]:
    """Run a bounded official IF probe after the capability gate passes.

    This intentionally accepts no fallback placer and performs no production
    backend selection.  Semantic `.phys` import remains a separate adapter
    requirement, so a successful result is diagnostic rather than qualifying.
    """

    require_dreamplacefpga_execution_ready(report)
    root = Path(str(report["runtime"]["root"]))
    device = Path(str(report["inputs"]["device"]["path"]))
    netlist = Path(str(report["inputs"]["logical_netlist"]["path"]))
    output_dir.mkdir(parents=True, exist_ok=False)
    config_path = output_dir / "dreamplacefpga.json"
    config = {
        "interchange_netlist": str(netlist),
        "interchange_device": str(device),
        "result_dir": str(output_dir / "results"),
        "gpu": 0,
        "num_bins_x": 64,
        "num_bins_y": 64,
        "global_place_stages": [{
            "num_bins_x": 64,
            "num_bins_y": 64,
            "iteration": 2000,
            "learning_rate": 0.01,
            "wirelength": "weighted_average",
            "optimizer": "nesterov",
        }],
        "routability_opt_flag": 0,
        "target_density": 0.75,
        "density_weight": 8e-5,
        "random_seed": 1,
        "scale_factor": 1.0,
        "global_place_flag": 1,
        "legalize_flag": 1,
        "detailed_place_flag": 0,
        "dtype": "float32",
        "plot_flag": 0,
        "num_threads": 1,
        "deterministic_flag": 1,
        "enable_if": 1,
        "enable_site_routing": 0,
    }
    write_json(config_path, config, compact=True)
    command = [
        str(python),
        str(root / "dreamplacefpga/Placer.py"),
        str(config_path),
        *extra_args,
    ]
    completed = subprocess.run(
        command,
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ValidationError(
            "DREAMPlaceFPGA standalone Interchange probe failed with exit code "
            f"{completed.returncode}: {completed.stderr[-2000:]}"
        )
    phys_outputs = sorted((output_dir / "results").rglob("*.phys"))
    if len(phys_outputs) != 1:
        raise ValidationError(
            "DREAMPlaceFPGA probe must produce exactly one .phys output"
        )
    _validate_phys_container(phys_outputs[0])
    return {
        "schema": DREAMPLACEFPGA_PROBE_SCHEMA,
        "status": "pass",
        "provider": "dreamplacefpga-interchange-candidate",
        "upstream_revision": DREAMPLACEFPGA_UPSTREAM_REVISION,
        "physical_netlist": str(phys_outputs[0]),
        "qualification_boundary": (
            "container-only standalone diagnostic; semantic placement import pending"
        ),
    }
