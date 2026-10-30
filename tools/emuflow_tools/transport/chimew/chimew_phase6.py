"""Bridge certified Chimew assignments to EmuFlow lane/electrical contracts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from emuflow_tools.transport.chimew.chimew_bank_channel import CHIMEW_BANK_CHANNEL_PROVIDER, CHIMEW_BANK_CHANNEL_REPORT_SCHEMA
from emuflow_tools.transport.chimew.chimew_bank_channel_storage import read_chimew_bank_channel_input
from emuflow_tools.transport.chimew.chimew_bank_channel_report_storage import BankChannelReportView, read_chimew_bank_channel_report, copy_chimew_bank_channel_report_storage
from emuflow_tools.transport.chimew.chimew_binding_storage import CHIMEW_BINDING_STORAGE_SCHEMA, ChimewBindingView, read_chimew_binding
from emuflow_tools.transport.chimew.chimew_qualification import CHIMEW_BYTE_BOUND_QUALIFICATION_SCHEMA, CHIMEW_BYTE_BOUND_QUALIFICATION_PROVIDER, CHIMEW_QUALIFICATION_SCHEMA, CHIMEW_QUALIFICATION_PROVIDER, canonical_sha256
from emuflow.common.errors import ValidationError
from emuflow.common.json_io import write_json
from emuflow_tools.document_io import read_json
from emuflow_tools.transport.pin_plan_storage import PIN_PLAN_STORAGE_SCHEMA, read_pin_plan
from emuflow.platform.platform import Platform
from emuflow_tools.transport.position_hints_storage import POSITION_HINTS_STORAGE_SCHEMA, read_position_hints


CHIMEW_ELECTRICAL_MAP_SCHEMA_V1 = "emuflow.chimew-electrical-channel-map/v1"
CHIMEW_ELECTRICAL_MAP_SCHEMA = "emuflow.chimew-electrical-channel-map/v2"
CHIMEW_ELECTRICAL_MAP_PROVIDER = (
    "source-qualified-boarddb-electrical-channel-map-v1"
)
CHIMEW_PHASE6_BINDING_SCHEMA_V1 = "emuflow.chimew-phase6-electrical-binding/v1"
CHIMEW_PHASE6_BINDING_SCHEMA = "emuflow.chimew-phase6-electrical-binding/v2"
CHIMEW_PHASE6_BINDING_PROVIDER = "chimew-paper-plus-emuflow-electrical-slot-v1"
CHIMEW_PHASE6_ADAPTER_REPORT_SCHEMA_V1 = (
    "emuflow.chimew-phase6-adapter-report/v1"
)
CHIMEW_PHASE6_ADAPTER_REPORT_SCHEMA_V2 = (
    "emuflow.chimew-phase6-adapter-report/v2"
)
CHIMEW_PHASE6_ADAPTER_REPORT_SCHEMA = "emuflow.chimew-phase6-adapter-report/v3"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _publish_chimew_adapter_file(source: Path, destination: Path) -> None:
    """Atomically publish one already-sealed adapter artifact."""

    os.replace(source, destination)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


_ADAPTER_GENERATION_PATTERN = re.compile(
    r"^(adapter_report|bank_channel_report|qualification_certificate|"
    r"position_hints|pin_plan|electrical_binding)\.([0-9a-f]{64})"
    r"\.(json|sqlite)$"
)
_ADAPTER_LEGACY_FIXED_NAMES = {
    "adapter_report.json",
    "bank_channel_report.json",
    "qualification_certificate.json",
    "position_hints.json",
    "pin_plan.json",
    "electrical_binding.json",
}
_ADAPTER_ARTIFACT_LAYOUT = {
    "bank_channel_report": ("bank_channel_report", "json"),
    "bank_channel_report_store": ("bank_channel_report", "sqlite"),
    "qualification_certificate": ("qualification_certificate", "json"),
    "position_hints": ("position_hints", "json"),
    "position_hints_store": ("position_hints", "sqlite"),
    "pin_plan": ("pin_plan", "json"),
    "pin_plan_store": ("pin_plan", "sqlite"),
    "electrical_binding": ("electrical_binding", "json"),
    "electrical_binding_store": ("electrical_binding", "sqlite"),
}


def chimew_adapter_report_generation_path(
    output_dir: Path, report: Mapping[str, Any]
) -> Path:
    """Return the immutable report-generation path for ``report``."""

    payload = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return Path(output_dir) / f"adapter_report.{digest}.json"


def validate_chimew_adapter_generation(report_path: Path) -> set[Path]:
    """Validate one complete immutable adapter generation as a group."""

    report_path = Path(report_path)
    match = _ADAPTER_GENERATION_PATTERN.fullmatch(report_path.name)
    if (
        match is None
        or match.group(1) != "adapter_report"
        or match.group(3) != "json"
        or not report_path.is_file()
        or _file_sha256(report_path) != match.group(2)
    ):
        raise ValidationError("Chimew adapter report generation is invalid")
    report = read_json(report_path)
    artifacts = report.get("artifacts")
    schema = report.get("schema")
    required_labels = set(_ADAPTER_ARTIFACT_LAYOUT) - {
        "qualification_certificate"
    }
    if schema != CHIMEW_PHASE6_ADAPTER_REPORT_SCHEMA:
        required_labels.remove("bank_channel_report_store")
    supplied_labels = set(artifacts) if isinstance(artifacts, dict) else set()
    if (
        schema
        not in {
            CHIMEW_PHASE6_ADAPTER_REPORT_SCHEMA_V2,
            CHIMEW_PHASE6_ADAPTER_REPORT_SCHEMA,
        }
        or report.get("status") != "pass"
        or report.get("provider") != CHIMEW_PHASE6_BINDING_PROVIDER
        or not isinstance(report.get("design"), str)
        or not isinstance(report.get("platform"), str)
        or not isinstance(artifacts, dict)
        or not required_labels <= supplied_labels
        or not supplied_labels <= set(_ADAPTER_ARTIFACT_LAYOUT)
    ):
        raise ValidationError("Chimew adapter report generation is malformed")

    paths = {"adapter_report": report_path.resolve()}
    for label, name in artifacts.items():
        artifact_match = (
            _ADAPTER_GENERATION_PATTERN.fullmatch(name)
            if isinstance(name, str) and Path(name).name == name
            else None
        )
        if (
            artifact_match is None
            or (artifact_match.group(1), artifact_match.group(3))
            != _ADAPTER_ARTIFACT_LAYOUT[label]
        ):
            raise ValidationError("Chimew adapter generation path is invalid")
        artifact_path = report_path.with_name(name)
        if (
            not artifact_path.is_file()
            or _file_sha256(artifact_path) != artifact_match.group(2)
        ):
            raise ValidationError("Chimew adapter generation seal differs")
        paths[label] = artifact_path.resolve()

    try:
        position_selector = read_json(paths["position_hints"])
        pin_selector = read_json(paths["pin_plan"])
        binding_selector = read_json(paths["electrical_binding"])
    except (OSError, ValueError) as error:
        raise ValidationError(
            "Chimew adapter selector generation is malformed"
        ) from error
    if (
        position_selector.get("schema") != POSITION_HINTS_STORAGE_SCHEMA
        or position_selector.get("artifact")
        != Path(paths["position_hints_store"]).name
        or pin_selector.get("schema") != PIN_PLAN_STORAGE_SCHEMA
        or pin_selector.get("artifact") != Path(paths["pin_plan_store"]).name
        or binding_selector.get("schema") != CHIMEW_BINDING_STORAGE_SCHEMA
        or binding_selector.get("artifact")
        != Path(paths["electrical_binding_store"]).name
    ):
        raise ValidationError("Chimew adapter selector/store pairing differs")
    try:
        read_position_hints(paths["position_hints"])
        read_pin_plan(paths["pin_plan"])
        read_chimew_binding(paths["electrical_binding"])
    except (OSError, ValueError, sqlite3.Error, ValidationError) as error:
        raise ValidationError(
            "Chimew adapter indexed generation is malformed"
        ) from error

    try:
        bank_report = read_chimew_bank_channel_report(
            paths["bank_channel_report"],
        )
    except (OSError, ValueError, ValidationError) as error:
        raise ValidationError(
            "Chimew bank/channel report generation is malformed"
        ) from error
    if (
        bank_report.get("schema") != CHIMEW_BANK_CHANNEL_REPORT_SCHEMA
        or bank_report.get("provider") != CHIMEW_BANK_CHANNEL_PROVIDER
        or bank_report.get("design") != report["design"]
        or bank_report.get("platform") != report["platform"]
        or not isinstance(bank_report.get("input_sha256"), str)
        or len(bank_report["input_sha256"]) != 64
        or not isinstance(bank_report.get("assignments"), Sequence)
        or not isinstance(bank_report.get("metrics"), dict)
        or not isinstance(bank_report.get("provenance"), dict)
    ):
        raise ValidationError("Chimew bank/channel report generation is malformed")
    if "bank_channel_report_store" in paths and (
        not isinstance(bank_report, BankChannelReportView)
        or bank_report.store_path != paths["bank_channel_report_store"]
    ):
        raise ValidationError("Chimew bank/channel report selector/store pairing differs")
    if isinstance(bank_report, BankChannelReportView):
        bank_report.close()

    if "qualification_certificate" in paths:
        try:
            certificate = read_json(paths["qualification_certificate"])
        except (OSError, ValueError) as error:
            raise ValidationError(
                "Chimew qualification generation is malformed"
            ) from error
        identity = (certificate.get("schema"), certificate.get("provider"))
        if (
            identity
            not in {
                (CHIMEW_QUALIFICATION_SCHEMA, CHIMEW_QUALIFICATION_PROVIDER),
                (
                    CHIMEW_BYTE_BOUND_QUALIFICATION_SCHEMA,
                    CHIMEW_BYTE_BOUND_QUALIFICATION_PROVIDER,
                ),
            }
            or certificate.get("status") != "pass"
            or certificate.get("design") != report["design"]
            or certificate.get("platform") != report["platform"]
        ):
            raise ValidationError("Chimew qualification generation is malformed")
        unsigned = dict(certificate)
        supplied_sha = unsigned.pop("qualification_sha256", None)
        if supplied_sha != canonical_sha256(unsigned):
            raise ValidationError("Chimew qualification generation seal differs")
    return set(paths.values())


def garbage_collect_chimew_adapter_generations(
    output_dir: Path,
    referenced_names: Sequence[str],
    *,
    additionally_retired_names: Sequence[str] = (),
) -> None:
    """Remove only unreferenced, validated immutable adapter generations.

    ``additionally_retired_names`` is reserved for names previously sealed by
    an outer pipeline report.  It permits removal of legacy fixed adapter
    artifacts only after that outer report has committed a new inventory;
    arbitrary extra files are never removed here.
    """

    output_dir = Path(output_dir)
    retained = {
        name
        for name in referenced_names
        if isinstance(name, str) and Path(name).name == name
    }
    retired = {
        name
        for name in additionally_retired_names
        if isinstance(name, str) and Path(name).name == name
    }
    if not output_dir.is_dir():
        return
    removable: set[Path] = set()
    for report_path in output_dir.glob("adapter_report.*.json"):
        try:
            group = validate_chimew_adapter_generation(report_path)
        except (OSError, ValueError, sqlite3.Error, ValidationError):
            continue
        if report_path.name not in retained:
            removable.update(
                path for path in group if path.name not in retained
            )
    for name in retired & _ADAPTER_LEGACY_FIXED_NAMES:
        path = output_dir / name
        if path.is_file() and name not in retained:
            removable.add(path)
    for path in removable:
        try:
            path.unlink()
        except OSError:
            # GC follows a complete commit and does not define its validity.
            pass


def validate_chimew_phase6_binding(schedule, platform, pin_plan, binding):
    from emuflow_tools.transport.pin_artifacts_native import validate_pin_artifacts
    paths = [getattr(value, "manifest_path", None) for value in (schedule, pin_plan, binding)]
    if any(path is None for path in paths):
        raise ValidationError("electrical binding validation requires current typed artifact owners")
    return validate_pin_artifacts(paths[0], platform, paths[1], binding_path=paths[2])["binding_validation"]


def _run_chimew_phase6_adapter_with_input(
    schedule_path: Path,
    platform_path: Path,
    assignment_input: Mapping[str, Any],
    electrical_map_path: Path,
    output_dir: Path,
    *,
    qualification_path: Optional[Path] = None,
    bank_channel_report_path: Optional[Path] = None,
    executable: Optional[str] = None,
    region_count: int = 31,
    defer_generation_gc: bool = False,
) -> Dict[str, Any]:
    """Publish one crash-safe, report-committed Phase 6 adapter bundle."""

    from emuflow_tools.transport.pin_artifacts_native import build_pin_artifacts, validate_pin_artifacts
    qualification = read_json(qualification_path) if qualification_path else None
    platform = Platform.load(platform_path)
    output_dir = Path(output_dir)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_container = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}.adapter-",
            suffix=".tmp",
            dir=output_dir.parent,
        )
    )
    staging_root = staging_container / "payload"
    installed: list[Path] = []
    report_committed = False
    previous_report: Mapping[str, Any] | None = None
    previous_names: tuple[Any, ...] = ()
    previous_report_path = output_dir / "adapter_report.json"
    if previous_report_path.is_file():
        try:
            candidate = read_json(previous_report_path)
            if isinstance(candidate.get("artifacts"), dict):
                previous_report = candidate
        except (OSError, ValueError):
            previous_report = None
    if not defer_generation_gc:
        previous_names = (
            (
                *previous_report["artifacts"].values(),
                chimew_adapter_report_generation_path(
                    output_dir, previous_report
                ).name,
            )
            if previous_report is not None
            else ()
        )
        garbage_collect_chimew_adapter_generations(
            output_dir,
            (*previous_names, "adapter_report.json"),
        )
    indexed_bank_report: BankChannelReportView | None = None
    result_bank_report: Mapping[str, Any] | None = None
    try:
        result = build_pin_artifacts(
            schedule_path, platform, assignment_input.manifest_path, electrical_map_path, staging_root,
            board_path=platform_path, qualification_path=qualification_path,
            bank_report_path=bank_channel_report_path, executable=executable, region_count=region_count,
        )
        indexed_binding = result["electrical_binding"]
        indexed_positions = result["position_hints"]
        indexed_plan = result["pin_plan"]
        if not isinstance(indexed_binding, ChimewBindingView) or not all(
            isinstance(getattr(value, "store_path", None), Path)
            for value in (indexed_positions, indexed_plan)
        ):
            raise ValidationError(
                "Chimew adapter did not publish indexed artifacts"
            )

        report_selector_path = staging_root / "bank_channel_report.json"
        result_bank_report = result["bank_channel_report"]
        if (
            isinstance(result_bank_report, BankChannelReportView)
            and result_bank_report.manifest_path == report_selector_path.resolve()
        ):
            indexed_bank_report = result_bank_report
        else:
            if not isinstance(result_bank_report, BankChannelReportView):
                raise ValidationError("Chimew adapter requires a typed assignment report")
            copy_chimew_bank_channel_report_storage(result_bank_report.manifest_path, report_selector_path)
            indexed_bank_report = read_chimew_bank_channel_report(report_selector_path)
        if qualification is not None:
            write_json(staging_root / "qualification_certificate.json", qualification)

        sources: Dict[str, Path] = {
            "bank_channel_report": staging_root / "bank_channel_report.json",
            "bank_channel_report_store": indexed_bank_report.store_path,
            "position_hints": indexed_positions.manifest_path,
            "position_hints_store": indexed_positions.store_path,
            "pin_plan": indexed_plan.manifest_path,
            "pin_plan_store": indexed_plan.store_path,
            "electrical_binding": indexed_binding.manifest_path,
            "electrical_binding_store": indexed_binding.store_path,
        }
        if qualification is not None:
            sources["qualification_certificate"] = (
                staging_root / "qualification_certificate.json"
            )

        sidecar_labels = {
            "bank_channel_report_store",
            "position_hints_store",
            "pin_plan_store",
            "electrical_binding_store",
        }
        artifact_names: Dict[str, str] = {}
        for label, source in sources.items():
            digest = _file_sha256(source)
            artifact_names[label] = (
                source.name
                if label in sidecar_labels
                else f"{source.stem}.{digest}{source.suffix}"
            )

        report = {
            "schema": CHIMEW_PHASE6_ADAPTER_REPORT_SCHEMA,
            "status": "pass",
            "provider": CHIMEW_PHASE6_BINDING_PROVIDER,
            "design": result["pin_plan"]["design"],
            "platform": result["pin_plan"]["platform"],
            "paper_provider": result["bank_channel_report"]["provider"],
            "validation": result["validation"],
            "electrical_metrics": result["electrical_binding"]["metrics"],
            "lookahead_qualification": (
                result["pin_plan"]["configuration"]["lookahead_qualification"]
            ),
            "qualification_validation": result["qualification_validation"],
            "artifacts": artifact_names,
        }
        staged_report = staging_root / "adapter_report.json"
        write_json(staged_report, report)

        output_dir.mkdir(parents=True, exist_ok=True)

        # Sidecars and selectors are immutable, content-addressed generations.
        # The adapter report is published after every referenced artifact.
        publish_order = [
            *sorted(sidecar_labels & sources.keys()),
            *sorted(set(sources) - sidecar_labels),
        ]
        for label in publish_order:
            source = sources[label]
            _fsync_file(source)
            destination = output_dir / artifact_names[label]
            if destination.is_file():
                if _file_sha256(destination) != _file_sha256(source):
                    raise ValidationError(
                        "Chimew adapter content-addressed artifact collision"
                    )
                source.unlink()
                continue
            _publish_chimew_adapter_file(source, destination)
            installed.append(destination)
        _fsync_directory(output_dir)

        # Reopen from final paths before committing the report.  This checks
        # relocation, all store seals and the independent schedule/pin/binding
        # joins while the previous report still names the previous generation.
        final_positions = read_position_hints(
            output_dir / artifact_names["position_hints"]
        )
        final_plan = read_pin_plan(output_dir / artifact_names["pin_plan"])
        final_binding = read_chimew_binding(
            output_dir / artifact_names["electrical_binding"]
        )
        checked = validate_pin_artifacts(schedule_path, platform,
            final_plan.manifest_path, positions_path=final_positions.manifest_path,
            binding_path=final_binding.manifest_path)
        if checked["pin_validation"] != result["validation"]:
            raise ValidationError("relocated Chimew pin-plan validation differs")

        _fsync_file(staged_report)
        report_destination = chimew_adapter_report_generation_path(
            output_dir, report
        )
        staged_fixed_report = staging_root / "adapter_report.fixed.json"
        if not defer_generation_gc:
            shutil.copy2(staged_report, staged_fixed_report)
            _fsync_file(staged_fixed_report)
        if report_destination.is_file():
            if _file_sha256(report_destination) != _file_sha256(staged_report):
                raise ValidationError(
                    "Chimew adapter report-generation collision"
                )
            staged_report.unlink()
        else:
            _publish_chimew_adapter_file(staged_report, report_destination)
            installed.append(report_destination)
        if not defer_generation_gc:
            _publish_chimew_adapter_file(
                staged_fixed_report, previous_report_path
            )
        report_committed = True
        _fsync_directory(output_dir)

        # Standalone adapters own their report commit and may collect every
        # older generation.  Pipeline-managed adapters defer collection until
        # the outer pipeline_report has committed its matching inventory.
        if not defer_generation_gc:
            garbage_collect_chimew_adapter_generations(
                output_dir,
                (
                    *artifact_names.values(),
                    report_destination.name,
                    "adapter_report.json",
                ),
                additionally_retired_names=previous_names,
            )
        return report
    except BaseException:
        if not report_committed:
            for path in reversed(installed):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        raise
    finally:
        if isinstance(indexed_bank_report, BankChannelReportView):
            indexed_bank_report.close()
        if (
            isinstance(result_bank_report, BankChannelReportView)
            and result_bank_report is not indexed_bank_report
        ):
            result_bank_report.close()
        shutil.rmtree(staging_container, ignore_errors=True)


def run_chimew_phase6_adapter(
    schedule_path: Path,
    platform_path: Path,
    bank_channel_input_path: Path,
    electrical_map_path: Path,
    output_dir: Path,
    *,
    qualification_path: Optional[Path] = None,
    bank_channel_report_path: Optional[Path] = None,
    executable: Optional[str] = None,
    region_count: int = 31,
    defer_generation_gc: bool = False,
) -> Dict[str, Any]:
    """Lease typed bank/group storage through the adapter's final cursor."""

    assignment_input = read_chimew_bank_channel_input(bank_channel_input_path)
    if not hasattr(assignment_input, "__enter__"):
        raise ValidationError("Phase 6 production requires indexed bank storage")
    with assignment_input:
        return _run_chimew_phase6_adapter_with_input(
            schedule_path,
            platform_path,
            assignment_input,
            electrical_map_path,
            output_dir,
            qualification_path=qualification_path,
            bank_channel_report_path=bank_channel_report_path,
            executable=executable,
            region_count=region_count,
            defer_generation_gc=defer_generation_gc,
        )
