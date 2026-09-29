"""Fail-closed native runner for the public basic AMF-Placer 2.0 core.

This module is intentionally an internal provider boundary.  It proves the
identity of a compiled upstream executable, exports the public AMF text
formats, invokes the real optimizer as a subprocess, imports its placement as
data, and independently certifies the result.  A unit-test subprocess may
exercise the protocol only when explicitly labelled ``test-double``; such a
run can never be reported as native qualification evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from .amf_adapter import (
    amf_assignment_sha256,
    export_amf_design,
    export_amf_fixture_device,
    import_amf_fixture_result,
)
from .amf_placer import AMF_PUBLIC_AUDITED_REVISION, AMF_PUBLIC_PROVIDER_ID
from .architecture import ArchitectureDB
from .errors import ValidationError
from .io import read_json, write_json
from .xilinx_placement import (
    XILINX_AMF_NATIVE_BRIDGE_PROVIDER,
    XILINX_PLACEMENT_SCHEMA,
    XILINX_ROUTE_A_SITE_UTILIZATION_LIMIT,
    validate_xilinx_placement,
)


AMF_EXECUTABLE_MANIFEST_SCHEMA = "emuflow.amf-native-executable/v1"
AMF_NATIVE_CERTIFICATE_SCHEMA = "emuflow.amf-native-placement/v1"
AMF_RAPIDWRIGHT_BRIDGE_SCHEMA = "emuflow.amf-rapidwright-bridge/v1"
AMF_PORTABILITY_PATCH_SHA256 = (
    "ffada114b9020097892c747dba18bf4c348b6b0c811bb87ba75ff2550a311133"
)

_RUNTIME_KINDS = {"upstream-native", "test-double"}
_STAGE_MARKERS = {
    "packing": "InitialPacker Finding unpacked units",
    "global_placement": "GlobalPlacer GlobalPlacement_CLBElements started",
    "detailed_placement": "ParallelCLBPacker: dumping placementTcl archieve",
    "complete": "Placement Done",
}
_RESULT_RE = re.compile(r"^(?P<prefix>.+)-first-(?P<index>\d+)\.tcl$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_digest(value: object, context: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValidationError(f"{context} must be a lowercase SHA-256 digest")
    return value


def _run_usage_probe(executable: Path, timeout_seconds: float) -> str:
    try:
        completed = subprocess.run(
            [str(executable)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValidationError(f"AMF executable probe failed: {error}") from error
    output = completed.stdout or ""
    if completed.returncode == 0 or "Usage:" not in output or "<config JSON file>" not in output:
        raise ValidationError("AMF executable does not expose the pinned usage contract")
    return hashlib.sha256(output.encode("utf-8")).hexdigest()


def probe_amf_native_executable(
    executable: Path,
    *,
    source_revision: str,
    portability_patch: Optional[Path] = None,
    runtime_kind: str = "upstream-native",
    timeout_seconds: float = 5.0,
) -> Dict[str, Any]:
    """Create a sealed executable manifest without claiming a placement run."""

    executable = executable.resolve()
    if runtime_kind not in _RUNTIME_KINDS:
        raise ValidationError("AMF executable runtime kind is invalid")
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValidationError("AMF executable is missing or not executable")
    if runtime_kind == "upstream-native":
        if source_revision != AMF_PUBLIC_AUDITED_REVISION:
            raise ValidationError("AMF upstream revision is not pinned")
        if portability_patch is None or not portability_patch.is_file():
            raise ValidationError("AMF native executable requires the reviewed portability patch")
        patch_sha256: Optional[str] = _sha256(portability_patch)
        if patch_sha256 != AMF_PORTABILITY_PATCH_SHA256:
            raise ValidationError("AMF portability patch is not the reviewed patch")
    else:
        if source_revision != "test-double":
            raise ValidationError("AMF test-double source identity is invalid")
        patch_sha256 = None
    result = {
        "schema": AMF_EXECUTABLE_MANIFEST_SCHEMA,
        "status": "pass",
        "provider": AMF_PUBLIC_PROVIDER_ID,
        "runtime_kind": runtime_kind,
        "source_revision": source_revision,
        "executable_sha256": _sha256(executable),
        "portability_patch_sha256": patch_sha256,
        "usage_probe_sha256": _run_usage_probe(executable, timeout_seconds),
    }
    return validate_amf_executable_manifest(result)


def validate_amf_executable_manifest(value: Mapping[str, Any]) -> Dict[str, Any]:
    if (
        value.get("schema") != AMF_EXECUTABLE_MANIFEST_SCHEMA
        or value.get("status") != "pass"
        or value.get("provider") != AMF_PUBLIC_PROVIDER_ID
    ):
        raise ValidationError("AMF executable manifest identity is invalid")
    runtime_kind = value.get("runtime_kind")
    if runtime_kind not in _RUNTIME_KINDS:
        raise ValidationError("AMF executable manifest runtime kind is invalid")
    _require_digest(value.get("executable_sha256"), "AMF executable digest")
    _require_digest(value.get("usage_probe_sha256"), "AMF usage probe digest")
    if runtime_kind == "upstream-native":
        if value.get("source_revision") != AMF_PUBLIC_AUDITED_REVISION:
            raise ValidationError("AMF executable manifest revision is invalid")
        if (
            _require_digest(value.get("portability_patch_sha256"), "AMF patch digest")
            != AMF_PORTABILITY_PATCH_SHA256
        ):
            raise ValidationError("AMF executable manifest patch is not reviewed")
    elif value.get("source_revision") != "test-double" or value.get("portability_patch_sha256") is not None:
        raise ValidationError("AMF test-double manifest identity is invalid")
    if set(value) != {
        "schema", "status", "provider", "runtime_kind", "source_revision",
        "executable_sha256", "portability_patch_sha256", "usage_probe_sha256",
    }:
        raise ValidationError("AMF executable manifest fields are invalid")
    return dict(value)


def _reject_unqualified_design(mapped: Mapping[str, Any], packed: Mapping[str, Any]) -> None:
    chains = packed.get("cascade_chains")
    if not isinstance(chains, list):
        raise ValidationError("AMF native input lacks packed cascade metadata")
    if chains:
        raise ValidationError(
            "AMF DSP/BRAM/URAM or inter-site carry cascade qualification is missing"
        )
    modules = mapped.get("modules")
    if not isinstance(modules, Mapping):
        raise ValidationError("AMF native mapped modules are invalid")
    for module in modules.values():
        if not isinstance(module, Mapping):
            continue
        cells = module.get("cells")
        if not isinstance(cells, Mapping):
            continue
        for name, cell in cells.items():
            if not isinstance(cell, Mapping) or cell.get("type") not in {"FDCE", "FDPE", "FDRE", "FDSE"}:
                continue
            connections = cell.get("connections")
            clock = connections.get("C") if isinstance(connections, Mapping) else None
            if clock not in (["0"], ["1"]):
                raise ValidationError(
                    f"AMF XCVU19P clock legality is unverified for cell {name!r}"
                )


def _write_zip(path: Path, member: str, text: str) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, text)


def _amf_config(run_dir: Path, device: Mapping[str, Any]) -> Dict[str, str]:
    compatibility = run_dir / "compatibility"
    result_prefix = run_dir / "result" / "DumpCLBPacking"
    result_prefix.parent.mkdir(parents=True, exist_ok=True)
    (run_dir / "dump").mkdir(parents=True, exist_ok=True)
    counts = Counter(item["type"] for item in device["sites"])
    return {
        "vivado extracted design information file": str(run_dir / "design.zip"),
        "vivado extracted device information file": str(run_dir / "device.zip"),
        "special pin offset info file": str(run_dir / "special-pin-offsets.txt"),
        "cellType2fixedAmo file": str(compatibility / "cellType2fixedAmo"),
        "cellType2sharedCellType file": str(compatibility / "cellType2sharedCellType"),
        "sharedCellType2BELtype file": str(compatibility / "sharedCellType2BELtype"),
        "mergedSharedCellType2sharedCellType": str(compatibility / "mergedSharedCellType2sharedCellType"),
        "DumpCLBPacking": str(result_prefix),
        "DumpLUTFFPair": str(run_dir / "dump" / "DumpLUTFFPair"),
        "DumpClockUtilization": "false",
        "GlobalPlacerPrintHPWL": "false",
        "ClockPeriod": "10",
        "Simulated Annealing restartNum": "1",
        "Simulated Annealing IterNum": "10",
        "RandomInitialPlacement": "true",
        "DrawNetAfterEachIteration": "false",
        "PseudoNetWeight": "0.0025",
        "GlobalPlacementIteration": "9",
        "clockRegionXNum": "1",
        "clockRegionYNum": "1",
        "clockRegionDSPNum": str(max(1, counts.get("DSP48E2", 0))),
        "clockRegionBRAMNum": str(max(1, counts.get("RAMBFIFO18", 0))),
        "jobs": "1",
        "y2xRatio": "1.0",
        "ClusterPlacerVerbose": "false",
        "GlobalPlacerVerbose": "false",
        "drawClusters": "false",
        "MKL": "false",
        "useUnconstrainedCG": "true",
        "dumpDirectory": str(run_dir / "dump"),
    }


def _prepare_inputs(run_dir: Path, design: Mapping[str, Any], device: Mapping[str, Any]) -> Path:
    _write_zip(run_dir / "design.zip", "design.txt", design["archive_text"])
    _write_zip(run_dir / "device.zip", "device.txt", device["archive_text"])
    compatibility = run_dir / "compatibility"
    compatibility.mkdir(parents=True, exist_ok=True)
    for name, text in device["compatibility_text"].items():
        (compatibility / name).write_text(text, encoding="utf-8")
    (run_dir / "special-pin-offsets.txt").write_text("", encoding="utf-8")
    config_path = run_dir / "config.json"
    config_path.write_text(
        json.dumps(_amf_config(run_dir, device), sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return config_path


def _result_path(run_dir: Path) -> Path:
    candidates = []
    for path in (run_dir / "result").glob("DumpCLBPacking-first-*.tcl"):
        match = _RESULT_RE.fullmatch(path.name)
        if match is not None:
            candidates.append((int(match.group("index")), path))
    if not candidates:
        raise ValidationError("AMF optimizer did not emit final packing Tcl")
    return max(candidates)[1]


def _local_summary(architecture: ArchitectureDB, sites: Sequence[str]) -> Dict[str, Any]:
    capacity: Counter = Counter()
    usage: Counter = Counter()
    for site in architecture.value["sites"]:
        region = site.get("physical_region")
        if not isinstance(region, Mapping):
            continue
        key = (region.get("slr"), region.get("clock_region"), site.get("type"))
        capacity[key] += 1
    for name in sites:
        site = architecture.site_named(name)
        if site is None:
            raise ValidationError(f"AMF placement uses unknown site {name!r}")
        region = site.get("physical_region")
        if isinstance(region, Mapping):
            usage[(region.get("slr"), region.get("clock_region"), site.get("type"))] += 1
    limits = {
        key: max(1, int(math.ceil(count * XILINX_ROUTE_A_SITE_UTILIZATION_LIMIT)))
        for key, count in capacity.items()
    }
    if any(usage[key] > limits[key] for key in usage):
        raise ValidationError("AMF placement exceeds the Route A local utilization limit")
    return {
        "clock_region_site_groups": len(capacity),
        "maximum_clock_region_site_utilization": max(
            (usage[key] / count for key, count in capacity.items()), default=None
        ),
        "maximum_clock_region_site_reservation": max(
            (usage[key] / limits[key] for key in capacity), default=None
        ),
    }


def _placement_from_result(
    result: Mapping[str, Any],
    packed: Mapping[str, Any],
    architecture_path: Path,
    packed_path: Path,
    architecture: ArchitectureDB,
) -> Dict[str, Any]:
    by_cluster: Dict[str, list[Dict[str, Any]]] = {}
    for assignment in result["assignments"]:
        by_cluster.setdefault(assignment["cluster"], []).append(dict(assignment))
    packed_by_id = {item["id"]: item for item in packed["clusters"]}
    entries = []
    for cluster_id in sorted(by_cluster):
        selected = sorted(by_cluster[cluster_id], key=lambda item: item["instance"])
        site_name = selected[0]["site"]
        site = architecture.site_named(site_name)
        assert site is not None
        template = site.get("template", site.get("type"))
        assignments = [
            {
                "instance": item["instance"],
                "cell_type": item["cell_type"],
                "bel": item["bel"],
                "placement_mode": template,
                "site": site_name,
            }
            for item in selected
        ]
        if cluster_id not in packed_by_id:
            raise ValidationError(f"AMF result has unknown cluster {cluster_id!r}")
        entries.append({
            "cluster": cluster_id,
            "site": site_name,
            "site_type": site["type"],
            "x": site["x"],
            "y": site["y"],
            "fixed": False,
            "physical_region": site.get("physical_region"),
            "assignments": assignments,
        })
    summary = _local_summary(architecture, [item["site"] for item in entries])
    summary.update({
        "clusters": len(entries),
        "cells": sum(len(item["assignments"]) for item in entries),
        "fixed_clusters": 0,
        "cascade_chains": len(packed.get("cascade_chains", [])),
        "mean_guidance_displacement": None,
        "max_guidance_displacement": None,
        "site_types": dict(sorted(Counter(item["site_type"] for item in entries).items())),
    })
    return {
        "schema": XILINX_PLACEMENT_SCHEMA,
        "status": "pass",
        "part": architecture.part,
        "provider": XILINX_AMF_NATIVE_BRIDGE_PROVIDER,
        "policy": {
            "clock_region_site_utilization_limit": XILINX_ROUTE_A_SITE_UTILIZATION_LIMIT,
            "capacity_rounding": "ceil-with-one-site-minimum",
            "packing": "emuflow-packed-site-netlist-authoritative-v1",
            "placement_certificate": AMF_NATIVE_CERTIFICATE_SCHEMA,
        },
        "source": {
            "packed_sha256": _sha256(packed_path),
            "architecture_sha256": _sha256(architecture_path),
            "guidance_sha256": None,
            "constraints_sha256": None,
        },
        "clusters": entries,
        "summary": summary,
    }


def _assignment_vector(placement: Mapping[str, Any]) -> list[Dict[str, Any]]:
    return [
        {
            "cluster": cluster["cluster"],
            "instance": assignment["instance"],
            "cell_type": assignment["cell_type"],
            "site": cluster["site"],
            "bel": assignment["bel"],
        }
        for cluster in placement["clusters"]
        for assignment in cluster["assignments"]
    ]


def validate_amf_native_certificate(
    certificate: Mapping[str, Any],
    *,
    executable: Path,
    manifest: Mapping[str, Any],
    packed_path: Path,
    architecture_path: Path,
    placement_path: Path,
    allow_test_double: bool = False,
) -> Dict[str, Any]:
    manifest = validate_amf_executable_manifest(manifest)
    if (
        certificate.get("schema") != AMF_NATIVE_CERTIFICATE_SCHEMA
        or certificate.get("status") != "pass"
        or certificate.get("provider") != AMF_PUBLIC_PROVIDER_ID
    ):
        raise ValidationError("AMF native certificate identity is invalid")
    runtime_kind = manifest["runtime_kind"]
    if certificate.get("runtime_kind") != runtime_kind:
        raise ValidationError("AMF native certificate runtime kind is invalid")
    if runtime_kind == "test-double" and not allow_test_double:
        raise ValidationError("AMF test-double certificate is not native evidence")
    if _sha256(executable.resolve()) != manifest["executable_sha256"]:
        raise ValidationError("AMF executable changed after probing")
    if certificate.get("executable") != {
        key: manifest[key]
        for key in (
            "runtime_kind", "source_revision", "executable_sha256",
            "portability_patch_sha256", "usage_probe_sha256",
        )
    }:
        raise ValidationError("AMF certificate executable seal is invalid")
    markers = certificate.get("stage_markers")
    if markers != list(_STAGE_MARKERS):
        raise ValidationError("AMF certificate stage coverage is incomplete")
    if certificate.get("rapidwright_bridge") != {
        "schema": AMF_RAPIDWRIGHT_BRIDGE_SCHEMA,
        "status": "ready-for-routing",
        "input_schema": XILINX_PLACEMENT_SCHEMA,
        "route_provider": "rapidwright-rwroute-2026.1.0",
        "routing_executed": False,
    }:
        raise ValidationError("AMF RapidWright bridge boundary is invalid")
    placement_report = validate_xilinx_placement(
        packed_path, architecture_path, placement_path
    )
    placement = read_json(placement_path)
    if certificate.get("placement_sha256") != _sha256(placement_path):
        raise ValidationError("AMF certificate placement digest is invalid")
    assignments = _assignment_vector(placement)
    if certificate.get("assignment_sha256") != amf_assignment_sha256(assignments):
        raise ValidationError("AMF certificate assignment seal is invalid")
    if certificate.get("summary") != {
        "clusters": placement_report["clusters"],
        "cells": placement_report["cells"],
        "cascade_chains": placement_report["cascade_chains"],
    }:
        raise ValidationError("AMF certificate summary is invalid")
    return {
        "schema": "emuflow.amf-native-placement-validation/v1",
        "status": "pass" if runtime_kind == "upstream-native" else "test-only",
        "runtime_kind": runtime_kind,
        "placement_sha256": certificate["placement_sha256"],
        "assignment_sha256": certificate["assignment_sha256"],
    }


def run_amf_native(
    *,
    mapped_path: Path,
    packed_path: Path,
    architecture_path: Path,
    executable: Path,
    manifest_path: Path,
    placement_path: Path,
    certificate_path: Path,
    work_root: Path,
    top: Optional[str] = None,
    timeout_seconds: float = 300.0,
    allow_test_double: bool = False,
) -> Dict[str, Any]:
    """Run the sealed AMF optimizer and emit compact, independently checked evidence."""

    manifest = validate_amf_executable_manifest(read_json(manifest_path))
    executable = executable.resolve()
    if _sha256(executable) != manifest["executable_sha256"]:
        raise ValidationError("AMF executable differs from its manifest")
    if manifest["runtime_kind"] == "test-double" and not allow_test_double:
        raise ValidationError("AMF test double cannot be used as native evidence")
    mapped = read_json(mapped_path)
    packed = read_json(packed_path)
    _reject_unqualified_design(mapped, packed)
    design = export_amf_design(mapped, top=top)
    device = export_amf_fixture_device(read_json(architecture_path))
    architecture = ArchitectureDB.load(architecture_path)
    work_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="amf-native-", dir=work_root))
    started = time.monotonic()
    try:
        config_path = _prepare_inputs(run_dir, design, device)
        try:
            completed = subprocess.run(
                [str(executable), str(config_path)],
                cwd=run_dir,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValidationError(f"AMF optimizer invocation failed: {error}") from error
        log = completed.stdout or ""
        if completed.returncode != 0:
            raise ValidationError(
                "AMF optimizer failed with return code "
                f"{completed.returncode}: {log[-2000:]}"
            )
        missing_markers = [name for name, marker in _STAGE_MARKERS.items() if marker not in log]
        if missing_markers:
            raise ValidationError(
                "AMF optimizer did not prove stages: " + ", ".join(missing_markers)
            )
        result = import_amf_fixture_result(
            _result_path(run_dir).read_text(encoding="utf-8"),
            mapped=mapped,
            packed=packed,
            architecture_value=architecture.value,
            top=top,
        )
        placement = _placement_from_result(
            result, packed, architecture_path, packed_path, architecture
        )
        write_json(placement_path, placement, compact=True)
        placement_report = validate_xilinx_placement(
            packed_path, architecture_path, placement_path
        )
        certificate = {
            "schema": AMF_NATIVE_CERTIFICATE_SCHEMA,
            "status": "pass",
            "provider": AMF_PUBLIC_PROVIDER_ID,
            "runtime_kind": manifest["runtime_kind"],
            "executable": {
                key: manifest[key]
                for key in (
                    "runtime_kind", "source_revision", "executable_sha256",
                    "portability_patch_sha256", "usage_probe_sha256",
                )
            },
            "stage_markers": list(_STAGE_MARKERS),
            "placement_sha256": _sha256(placement_path),
            "assignment_sha256": amf_assignment_sha256(_assignment_vector(placement)),
            "summary": {
                "clusters": placement_report["clusters"],
                "cells": placement_report["cells"],
                "cascade_chains": placement_report["cascade_chains"],
            },
            "runtime_seconds": time.monotonic() - started,
            "rapidwright_bridge": {
                "schema": AMF_RAPIDWRIGHT_BRIDGE_SCHEMA,
                "status": "ready-for-routing",
                "input_schema": XILINX_PLACEMENT_SCHEMA,
                "route_provider": "rapidwright-rwroute-2026.1.0",
                "routing_executed": False,
            },
        }
        write_json(certificate_path, certificate, compact=True)
        validation = validate_amf_native_certificate(
            certificate,
            executable=executable,
            manifest=manifest,
            packed_path=packed_path,
            architecture_path=architecture_path,
            placement_path=placement_path,
            allow_test_double=allow_test_double,
        )
        return {"certificate": certificate, "validation": validation}
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
