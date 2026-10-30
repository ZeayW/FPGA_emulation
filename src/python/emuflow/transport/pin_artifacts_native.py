"""Bounded file-reference hosts for native pin/position/electrical artifacts."""
from __future__ import annotations
import json
import os
from pathlib import Path
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation, resolve_native_executable
from emuflow.common.tool_runtime import NativeToolProcess

_FIELDS = ("device", "inode", "bytes", "mtime_seconds", "mtime_nanoseconds", "ctime_seconds", "ctime_nanoseconds")
_SCHEMAS = {"build-pin-artifacts": "chimew-pin-artifacts-input-v1", "validate-pin-artifacts": "chimew-pin-validation-input-v1", "audit-electrical-map": "chimew-electrical-validation-input-v1"}


def _row(path, schema):
    return {"path": str(Path(path).resolve()), "schema": schema}


def _board_sources(platform, root, board_path=None):
    from emuflow.transport.board_configuration import _wiring_file
    from emuflow.transport.virtual_board import INVENTORY_PATH, FIXED_WIRING_ROOT
    board = platform.to_dict()
    if board_path is None:
        board_path = root / "board.json"
        board_path.write_text(json.dumps(board, sort_keys=True, allow_nan=False), encoding="utf-8")
    sources = {"board": _row(board_path, "boarddb-v1")}
    if platform.virtual_board_model is not None:
        sources["inventory"] = _row(INVENTORY_PATH, "hp-package-pin-inventory-v1")
        wiring = _wiring_file(board, FIXED_WIRING_ROOT)
        if wiring is not None:
            sources["wiring"] = _row(wiring, "fixed-wiring-csv-v1")
    return sources


def _invoke(command, arguments, root, expected, output_count):
    native = resolve_native_executable("emuflow-native")
    receipt = root / "operation.json"
    with NativeToolProcess(native, [native, "--report", str(receipt), "phase6", command, *map(str, arguments)],
            environment=dict(os.environ), scratch_parent=root) as process:
        completed = process.finish()
    if completed.returncode:
        message = completed.stdout[-4096:]
        if receipt.is_file() and receipt.stat().st_size <= 2 * 1024 * 1024:
            message = json.loads(receipt.read_text()).get("error", {}).get("message", message)
        raise ValidationError("native pin artifacts failed: " + message)
    if not receipt.is_file() or receipt.stat().st_size > 2 * 1024 * 1024:
        raise ValidationError("native pin receipt is missing or exceeds bound")
    operation = json.loads(receipt.read_text())
    if (operation.get("schema") != "emuflow.native-stage-result/v1" or operation.get("stage") != "phase6." + command
            or operation.get("status") != "pass" or len(operation.get("outputs", [])) != output_count):
        raise ValidationError("native pin receipt identity differs")
    observed = {}
    for row in operation.get("inputs", []):
        generation = tuple(row["generation"][key] for key in _FIELDS)
        identity = (row["path"], row["schema"])
        if native_file_generation(row["path"]) != generation or (identity in observed and observed[identity] != generation):
            raise ValidationError("native pin source generation changed")
        observed[identity] = generation
    if any(observed.get(key) != generation for key, generation in expected.items()):
        raise ValidationError("native pin source coverage differs")
    for row in operation["outputs"]:
        if native_file_generation(row["path"]) != tuple(row["generation"][key] for key in _FIELDS):
            raise ValidationError("native pin output generation changed")
    return operation


def _run(command, sources, request, destination, root, output_count=1):
    source = root / "request.json"
    source.write_text(json.dumps(request, sort_keys=True, allow_nan=False), encoding="utf-8")
    expected = {(v["path"], v["schema"]): native_file_generation(v["path"]) for v in sources.values()}
    expected[(str(source), _SCHEMAS[command])] = native_file_generation(source)
    return _invoke(command, [source, destination], root, expected, output_count)


def _summary(path, schema):
    if not path.is_file() or path.stat().st_size > 262144:
        raise ValidationError("native pin summary is missing or exceeds bound")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != schema or value.get("status") != "pass":
        raise ValidationError("native pin summary identity differs")
    return value


def build_pin_artifacts(schedule_path, platform, bank_input_path, electrical_path, output_dir, *, board_path=None,
                        qualification_path=None, bank_report_path=None, region_count=31, executable=None):
    from emuflow.transport.position_hints_storage import read_position_hints
    from emuflow.transport.pin_plan_storage import read_pin_plan
    from emuflow.transport.chimew.chimew_binding_storage import read_chimew_binding
    from emuflow.transport.chimew.chimew_bank_channel_report_storage import read_chimew_bank_channel_report
    if executable is not None:
        try:
            same = Path(executable).samefile(resolve_native_executable("emuflow-native"))
        except OSError:
            same = False
        if not same:
            raise ValidationError("Chimew pin planning requires the unified native executable")
    output = Path(output_dir).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".pin-host-", dir=output.parent) as temporary:
        root = Path(temporary).resolve()
        sources = _board_sources(platform, root, board_path)
        sources.update(schedule=_row(schedule_path, "physical-input-document-v1"), bank_input=_row(bank_input_path, "chimew-bank-input-selector-v1"), electrical=_row(electrical_path, "physical-input-document-v1"))
        if qualification_path is not None:
            sources["qualification"] = _row(qualification_path, "chimew-qualification-v1")
        if bank_report_path is not None:
            sources["bank_report"] = _row(bank_report_path, "chimew-bank-report-selector-v1")
        request = {"schema": "emuflow.chimew-pin-artifacts-input/v1", "sources": sources, "region_count": region_count, "verify_board_bytes": board_path is not None}
        operation = _run("build-pin-artifacts", sources, request, output, root, output_count=9)
        summary = _summary(output / "pin_artifacts.json", "emuflow.chimew-pin-artifacts/v1")
        result = {"status": "pass", "provider": summary["provider"], "validation": summary["validation"], "qualification_validation": summary["qualification_validation"],
                  "position_hints": read_position_hints(output / "position_hints.json"), "pin_plan": read_pin_plan(output / "pin_plan.json"),
                  "electrical_binding": read_chimew_binding(output / "electrical_binding.json"), "bank_channel_report": read_chimew_bank_channel_report(output / "bank_channel_report.json")}
        try:
            if result["pin_plan"]["design"] != summary["design"] or result["pin_plan"]["platform"] != platform.name:
                raise ValidationError("native pin publication owner identity differs")
            for row in operation["outputs"]:
                if native_file_generation(row["path"]) != tuple(row["generation"][key] for key in _FIELDS):
                    raise ValidationError("native pin publication changed")
            return result
        except BaseException:
            result["bank_channel_report"].close()
            raise


def validate_pin_artifacts(schedule_path, platform, plan_path, *, positions_path=None, binding_path=None):
    with tempfile.TemporaryDirectory(prefix=".pin-validation-") as temporary:
        root = Path(temporary).resolve()
        sources = _board_sources(platform, root)
        sources.update(schedule=_row(schedule_path, "physical-input-document-v1"), plan=_row(plan_path, "pin-plan-selector-v1"))
        if positions_path is not None:
            sources["positions"] = _row(positions_path, "position-hints-selector-v1")
        if binding_path is not None:
            sources["binding"] = _row(binding_path, "chimew-binding-selector-v1")
        output = root / "validation.json"
        _run("validate-pin-artifacts", sources, {"schema": "emuflow.chimew-pin-validation-input/v1", "sources": sources}, output, root)
        return _summary(output, "emuflow.chimew-pin-validation/v1")


def audit_electrical_map(bank_input_path, electrical_path, platform):
    with tempfile.TemporaryDirectory(prefix=".electrical-validation-") as temporary:
        root = Path(temporary).resolve()
        sources = _board_sources(platform, root)
        sources.update(bank_input=_row(bank_input_path, "chimew-bank-input-selector-v1"), electrical=_row(electrical_path, "physical-input-document-v1"))
        output = root / "validation.json"
        _run("audit-electrical-map", sources, {"schema": "emuflow.chimew-electrical-validation-input/v1", "sources": sources}, output, root)
        return _summary(output, "emuflow.chimew-electrical-validation/v1")


def copy_pin_artifact(selector, kind, output):
    selector, output = Path(selector).resolve(), Path(output).resolve()
    schema = {"pin-plan-store-v1": "pin-plan-selector-v1", "position-hints-store-v1": "position-hints-selector-v1", "chimew-binding-store-v1": "chimew-binding-selector-v1"}[kind]
    expected = {(str(selector), schema): native_file_generation(selector)}
    with tempfile.TemporaryDirectory(prefix=".pin-copy-") as temporary:
        root = Path(temporary).resolve()
        operation = _invoke("copy-pin-artifact", [kind, selector, output], root, expected, 2)
        if operation["outputs"][1]["path"] != str(output):
            raise ValidationError("native pin copy output differs")
        return operation


def read_pin_manifest(path, kind):
    """Audit the complete native owner, then retain only its bounded metadata."""
    from emuflow.storage.store_publication import bind_store_source, require_store_sources_unchanged
    path = Path(path).resolve()
    operation = copy_pin_artifact(path, kind, path)
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValidationError("pin artifact selector exceeds bound")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    payload = path.with_name(manifest["artifact"])
    store = bind_store_source(payload, kind)
    selector_schema = {"pin-plan-store-v1": "pin-plan-selector-v1", "position-hints-store-v1": "position-hints-selector-v1", "chimew-binding-store-v1": "chimew-binding-selector-v1"}[kind]
    selector = bind_store_source(path, selector_schema)
    for source, row in zip((store, selector), operation["outputs"]):
        if (str(source.path) != row["path"] or source.schema != row["schema"]
                or source.generation != tuple(row["generation"][key] for key in _FIELDS)):
            raise ValidationError("pin artifact source binding changed")
    require_store_sources_unchanged((selector, store))
    return manifest, (selector, store)
