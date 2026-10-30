"""Bounded hosts and certificate seals for native Chimew qualification.

The core owns record coverage, independent cost and RUDY reconstruction,
provenance joins and certificate generation. Python exchanges file references
and bounded receipts; it retains only finite certificate binding checks.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Mapping

from emuflow.common.errors import ValidationError


CHIMEW_QUALIFICATION_SCHEMA = "emuflow.chimew-phase6-qualification/v1"
CHIMEW_QUALIFICATION_PROVIDER = (
    "chimew-paper-kernel-chain-plus-emuflow-provenance-v1"
)
CHIMEW_BYTE_BOUND_QUALIFICATION_SCHEMA = "emuflow.chimew-phase6-qualification/v2"
CHIMEW_BYTE_BOUND_QUALIFICATION_PROVIDER = (
    "chimew-paper-kernel-chain-plus-emuflow-byte-provenance-v2"
)
CHIMEW_BYTE_BOUND_SOURCE_SCOPE = "byte-bound-source-artifacts"
CHIMEW_BYTE_BOUND_SOURCE_LABELS = {
    "routing",
    "placement",
    "netlist",
    "architecture",
    "package_pins",
}
CHIMEW_BYTE_BOUND_OPTIONAL_SOURCE_LABELS = {"timing_paths"}


def canonical_sha256(document: Mapping[str, Any]) -> str:
    from emuflow_tools.transport.chimew.chimew_bank_channel_storage import BankChannelInputView, canonical_bank_channel_input_sha256
    from emuflow.scheduling.tdm_schedule_storage import TDMScheduleView, canonical_tdm_schedule_sha256
    from emuflow_tools.transport.chimew.chimew_bank_channel_report_storage import BankChannelReportView, canonical_bank_channel_report_sha256

    if isinstance(document, BankChannelInputView):
        return canonical_bank_channel_input_sha256(document)
    if isinstance(document, TDMScheduleView):
        return canonical_tdm_schedule_sha256(document)
    if isinstance(document, BankChannelReportView):
        return canonical_bank_channel_report_sha256(document)
    payload = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _require_identity(
    schedule: Mapping[str, Any], label: str, document: Mapping[str, Any]
) -> None:
    if (
        document.get("design") != schedule.get("design")
        or document.get("platform") != schedule.get("platform")
    ):
        raise ValidationError(f"Chimew {label} identity does not match the schedule")


def _validate_source_binding(source_binding: Mapping[str, Any]) -> Dict[str, Any]:
    if (
        set(source_binding) != {"scope", "digests"}
        or source_binding.get("scope") != CHIMEW_BYTE_BOUND_SOURCE_SCOPE
    ):
        raise ValidationError("Chimew source binding scope is invalid")
    digests = source_binding.get("digests")
    if (
        not isinstance(digests, dict)
        or not CHIMEW_BYTE_BOUND_SOURCE_LABELS <= set(digests)
        or set(digests)
        - (
            CHIMEW_BYTE_BOUND_SOURCE_LABELS
            | CHIMEW_BYTE_BOUND_OPTIONAL_SOURCE_LABELS
        )
    ):
        raise ValidationError("Chimew source binding inventory is invalid")
    for label, digest in digests.items():
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValidationError(f"Chimew {label} source binding digest is invalid")
    return {"scope": CHIMEW_BYTE_BOUND_SOURCE_SCOPE, "digests": dict(digests)}


def validate_chimew_bank_channel_report_artifact(
    bank_input: Mapping[str, Any], report: Mapping[str, Any]
) -> Dict[str, Any]:
    """Check source identity, coverage, costs and direction layout in the core."""
    import os
    from pathlib import Path
    import tempfile
    from emuflow.common.native_tools import native_file_generation, resolve_native_executable
    from emuflow.common.tool_runtime import NativeToolProcess
    from emuflow.storage.store_publication import bind_view_sources, require_store_sources_unchanged
    from emuflow_tools.transport.chimew.chimew_bank_channel_storage import BankChannelInputView
    from emuflow_tools.transport.chimew.chimew_bank_channel_report_storage import BankChannelReportView

    if not isinstance(bank_input, BankChannelInputView) or not isinstance(report, BankChannelReportView):
        raise ValidationError("Chimew bank validation requires typed input and report owners")
    sources = bind_view_sources((bank_input, "chimew-bank-input-store-v1"),
                                (report, "chimew-bank-report-store-v1"))
    native = resolve_native_executable("emuflow-native")
    with tempfile.TemporaryDirectory(prefix=".chimew-bank-qualification-") as temporary:
        root = Path(temporary).resolve()
        receipt, summary = root / "operation.json", root / "validation.json"
        with NativeToolProcess(native, [native, "--report", str(receipt), "phase6",
                "audit-bank-assignment", str(bank_input.manifest_path), str(report.manifest_path), str(summary)],
                environment=dict(os.environ), scratch_parent=root) as process:
            completed = process.finish()
        if not receipt.is_file() or receipt.stat().st_size > 1024 * 1024:
            raise ValidationError("native Chimew bank validation receipt is missing or exceeds bound")
        operation = json.loads(receipt.read_text())
        if completed.returncode:
            raise ValidationError("native Chimew bank validation failed: " +
                operation.get("error", {}).get("message", completed.stdout))
        if (operation.get("schema") != "emuflow.native-stage-result/v1"
                or operation.get("stage") != "phase6.audit-bank-assignment"
                or operation.get("status") != "pass"
                or len(operation.get("inputs", [])) != 4
                or len(operation.get("outputs", [])) != 1):
            raise ValidationError("native Chimew bank validation identity disagrees")
        fields = ("device", "inode", "bytes", "mtime_seconds", "mtime_nanoseconds",
                  "ctime_seconds", "ctime_nanoseconds")
        expected = {(str(source.path), source.schema): source.generation for source in sources}
        observed = {}
        for row in operation["inputs"]:
            key = (row.get("path"), row.get("schema"))
            generation = tuple(row["generation"][field] for field in fields)
            if key in observed or key not in expected or generation != expected[key]:
                raise ValidationError("native Chimew bank validation source differs")
            observed[key] = generation
        output = operation["outputs"][0]
        if (observed != expected or output.get("path") != str(summary)
                or output.get("schema") != "chimew-bank-validation-v1"
                or not summary.is_file() or summary.stat().st_size > 262144
                or native_file_generation(summary) != tuple(output["generation"][field] for field in fields)):
            raise ValidationError("native Chimew bank validation evidence differs")
        checked = json.loads(summary.read_text())
        if (checked.get("schema") != "emuflow.chimew-bank-assignment-validation/v1"
                or checked.get("status") != "pass"
                or checked.get("input_sha256") != canonical_sha256(bank_input)
                or checked.get("report_sha256") != canonical_sha256(report)):
            raise ValidationError("native Chimew bank validation logical binding differs")
        require_store_sources_unchanged(sources)
    return checked


_CHAIN_ARTIFACTS = frozenset({
    "schedule", "crossings", "initial_grouping", "positions", "refined_grouping",
    "rudy_input", "rudy_report", "bank_channel_input", "bank_channel_report",
})


def qualify_chimew_artifact_chain(artifacts, output, *, source_binding=None):
    """Publish a native certificate from file owners and a bounded request."""
    import os
    import tempfile
    from pathlib import Path
    from emuflow.common.native_tools import native_file_generation, resolve_native_executable
    from emuflow.common.tool_runtime import NativeToolProcess

    if set(artifacts) != _CHAIN_ARTIFACTS:
        raise ValidationError("Chimew qualification artifact inventory differs")
    sources = {}
    for key, value in artifacts.items():
        path = Path(value).resolve()
        schema = ("chimew-bank-input-selector-v1" if key == "bank_channel_input" else
                  "chimew-bank-report-selector-v1" if key == "bank_channel_report" else
                  "physical-input-document-v1")
        sources[key] = {"path": str(path), "schema": schema}
    expected = {(v["path"], v["schema"]): native_file_generation(Path(v["path"]))
                for v in sources.values()}
    target = Path(output).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    native = resolve_native_executable("emuflow-native")
    with tempfile.TemporaryDirectory(prefix=".chimew-chain-host-", dir=target.parent) as temporary:
        root = Path(temporary).resolve()
        request, receipt = root / "request.json", root / "operation.json"
        payload = {"schema": "emuflow.chimew-qualification-input/v1", "artifacts": sources}
        if source_binding is not None:
            payload["source_binding"] = _validate_source_binding(source_binding)
        request.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        expected[(str(request), "chimew-qualification-input-v1")] = native_file_generation(request)
        with NativeToolProcess(native, [native, "--report", str(receipt), "phase6",
                "qualify-chimew-chain", str(request), str(target)],
                environment=dict(os.environ), scratch_parent=root) as process:
            completed = process.finish()
        if not receipt.is_file() or receipt.stat().st_size > 1024 * 1024:
            raise ValidationError("native Chimew chain receipt is missing or exceeds bound: " + completed.stdout[-4096:])
        operation = json.loads(receipt.read_text())
        if completed.returncode:
            raise ValidationError("native Chimew chain qualification failed: " +
                operation.get("error", {}).get("message", completed.stdout))
        if (operation.get("schema") != "emuflow.native-stage-result/v1"
                or operation.get("stage") != "phase6.qualify-chimew-chain"
                or operation.get("status") != "pass"
                or len(operation.get("outputs", [])) != 1):
            raise ValidationError("native Chimew chain receipt identity differs")
        fields = ("device", "inode", "bytes", "mtime_seconds", "mtime_nanoseconds",
                  "ctime_seconds", "ctime_nanoseconds")
        observed = {}
        for row in operation.get("inputs", []):
            key = (row.get("path"), row.get("schema"))
            generation = tuple(row["generation"][field] for field in fields)
            if native_file_generation(Path(row["path"])) != generation:
                raise ValidationError("native Chimew chain input generation changed")
            if key in observed and observed[key] != generation:
                raise ValidationError("native Chimew chain has mixed source generations")
            observed[key] = generation
        if any(observed.get(key) != generation for key, generation in expected.items()):
            raise ValidationError("native Chimew chain source coverage differs")
        result = operation["outputs"][0]
        if (result.get("path") != str(target)
                or result.get("schema") != "chimew-qualification-v1"
                or not target.is_file() or target.stat().st_size > 262144
                or native_file_generation(target) != tuple(result["generation"][field] for field in fields)):
            raise ValidationError("native Chimew chain output evidence differs")
        certificate = json.loads(target.read_text(encoding="utf-8"))
        unsigned = dict(certificate)
        supplied = unsigned.pop("qualification_sha256", None)
        if supplied != canonical_sha256(unsigned) or certificate.get("status") != "pass":
            raise ValidationError("native Chimew chain certificate self-seal differs")
        return certificate


def validate_chimew_phase6_qualification(certificate, artifacts, *, source_binding=None):
    """Independently reconstruct the certificate in the core and compare seals."""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory(prefix=".chimew-chain-check-") as temporary:
        expected = qualify_chimew_artifact_chain(artifacts, Path(temporary) / "qualification.json",
                                                 source_binding=source_binding)
    if dict(certificate) != expected:
        raise ValidationError("Chimew Phase 6 qualification certificate does not agree")
    return {
        "status": "pass", "signals": expected["metrics"]["signals"],
        "groups": expected["metrics"]["groups"],
        "qualification_scope": CHIMEW_BYTE_BOUND_SOURCE_SCOPE if source_binding is not None else "declared-digest-artifact-chain",
        "qualification_sha256": expected["qualification_sha256"],
    }


def validate_chimew_qualification_seal(
    certificate: Mapping[str, Any], schedule: Mapping[str, Any]
) -> Dict[str, Any]:
    """Validate the certificate identity and self-seal without source artifacts."""
    identity = (certificate.get("schema"), certificate.get("provider"))
    if identity not in {
        (CHIMEW_QUALIFICATION_SCHEMA, CHIMEW_QUALIFICATION_PROVIDER),
        (
            CHIMEW_BYTE_BOUND_QUALIFICATION_SCHEMA,
            CHIMEW_BYTE_BOUND_QUALIFICATION_PROVIDER,
        ),
    } or certificate.get("status") != "pass":
        raise ValidationError("Chimew Phase 6 qualification certificate is invalid")
    _require_identity(schedule, "qualification certificate", certificate)
    supplied_sha = certificate.get("qualification_sha256")
    unsigned = dict(certificate)
    unsigned.pop("qualification_sha256", None)
    if supplied_sha != canonical_sha256(unsigned):
        raise ValidationError("Chimew Phase 6 qualification self-seal is invalid")
    if identity[0] == CHIMEW_BYTE_BOUND_QUALIFICATION_SCHEMA:
        source_binding = certificate.get("source_binding")
        if not isinstance(source_binding, dict):
            raise ValidationError("Chimew byte-bound source binding is missing")
        validated_source_binding = _validate_source_binding(source_binding)
        if certificate.get("source_binding_sha256") != canonical_sha256(
            validated_source_binding
        ):
            raise ValidationError("Chimew byte-bound source binding seal is invalid")
    elif "source_binding" in certificate or "source_binding_sha256" in certificate:
        raise ValidationError("legacy Chimew qualification contains a source binding")
    artifacts = certificate.get("artifacts")
    provenance = certificate.get("provenance")
    if (
        not isinstance(artifacts, dict)
        or not isinstance(provenance, dict)
        or artifacts.get("schedule") != canonical_sha256(schedule)
    ):
        raise ValidationError("Chimew Phase 6 qualification schedule binding is broken")
    if identity[0] == CHIMEW_BYTE_BOUND_QUALIFICATION_SCHEMA and any(
        validated_source_binding["digests"][label] != provenance.get(field)
        for label, field in {
            "routing": "routing_sha256",
            "placement": "placement_sha256",
            "netlist": "netlist_sha256",
            "architecture": "architecture_sha256",
        }.items()
    ):
        raise ValidationError("Chimew byte-bound source provenance is inconsistent")
    metrics = certificate.get("metrics")
    if (
        not isinstance(metrics, dict)
        or metrics.get("artifact_chain_disagreements") != 0
        or metrics.get("rudy_overloaded_bins") != 0
    ):
        raise ValidationError("Chimew Phase 6 qualification gates did not pass")
    return {
        "status": "pass",
        "qualification_sha256": supplied_sha,
        "qualification_scope": (
            CHIMEW_BYTE_BOUND_SOURCE_SCOPE
            if identity[0] == CHIMEW_BYTE_BOUND_QUALIFICATION_SCHEMA
            else "declared-digest-artifact-chain"
        ),
        "signals": metrics.get("signals"),
        "groups": metrics.get("groups"),
    }


def validate_chimew_qualification_binding(
    certificate: Mapping[str, Any],
    schedule: Mapping[str, Any],
    bank_input: Mapping[str, Any],
) -> Dict[str, Any]:
    """Validate the self-sealed subset needed by the Phase 6 adapter."""

    validation = validate_chimew_qualification_seal(certificate, schedule)
    artifacts = certificate["artifacts"]
    provenance = certificate["provenance"]
    bank_provenance = bank_input.get("provenance")
    if (
        not isinstance(bank_provenance, dict)
        or artifacts.get("bank_channel_input") != canonical_sha256(bank_input)
        or artifacts.get("refined_grouping")
        != bank_provenance.get("grouping_sha256")
        or provenance.get("placement_sha256")
        != bank_provenance.get("placement_sha256")
        or provenance.get("architecture_sha256")
        != bank_provenance.get("architecture_sha256")
    ):
        raise ValidationError("Chimew Phase 6 qualification binding is broken")
    return validation
