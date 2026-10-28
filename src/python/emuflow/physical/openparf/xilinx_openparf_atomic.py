"""Bounded OpenPARF host coordination; all placement data belongs to the core."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256, read_json, write_json
from emuflow.physical.openparf.openparf import run_openparf, validate_openparf_runtime
from emuflow.physical.openparf.openparf_native_driver import native_metrics_path, validate_openparf_native_metrics
from emuflow.physical.openparf.placement_native import import_placement
OPENPARF_RESULT_CHECKPOINT_SCHEMA = "emuflow.openparf-result-checkpoint/v1"

def _sha256(path: Path) -> str:
    return file_sha256(path)


def _openparf_input_fingerprints(output_dir: Path) -> Dict[str, str]:
    """Seal every exported input that can affect one native placement."""

    manifest_path = output_dir / "manifest.json"
    manifest = read_json(manifest_path)
    files = manifest.get("files")
    if not isinstance(files, list) or any(not isinstance(item, str) for item in files):
        raise ValidationError("OpenPARF manifest file inventory is invalid")
    tracked = sorted(set(files) | {"manifest.json"})
    result: Dict[str, str] = {}
    for relative in tracked:
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValidationError("OpenPARF manifest contains an unsafe input path")
        path = output_dir / candidate
        if not path.is_file():
            raise ValidationError(f"OpenPARF input is missing: {relative}")
        if relative == "openparf.json":
            config = read_json(path)

            def relocate(value: Any) -> Any:
                if isinstance(value, Mapping):
                    return {
                        str(key): relocate(item) for key, item in value.items()
                    }
                if isinstance(value, list):
                    return [relocate(item) for item in value]
                if isinstance(value, str):
                    candidate_path = Path(value)
                    if candidate_path.is_absolute():
                        try:
                            relative_path = candidate_path.relative_to(
                                output_dir.resolve()
                            )
                        except ValueError:
                            return value
                        return "$OPENPARF_OUTPUT/" + str(relative_path)
                return value

            serialized = json.dumps(
                relocate(config), sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
            result[relative] = hashlib.sha256(serialized).hexdigest()
        else:
            result[relative] = _sha256(path)
    return result


def _reuse_openparf_result(output_dir: Path) -> Optional[Path]:
    """Return a prior result only when all input and output bytes still match."""

    checkpoint_path = output_dir / "openparf-result-checkpoint.json"
    if not checkpoint_path.is_file():
        return None
    try:
        checkpoint = read_json(checkpoint_path)
        if checkpoint.get("schema") != OPENPARF_RESULT_CHECKPOINT_SCHEMA:
            return None
        if checkpoint.get("input_sha256") != _openparf_input_fingerprints(output_dir):
            return None
        placement_relative = checkpoint.get("placement")
        metrics_relative = checkpoint.get("metrics")
        if not isinstance(placement_relative, str) or not isinstance(metrics_relative, str):
            return None
        placement_candidate = Path(placement_relative)
        metrics_candidate = Path(metrics_relative)
        if (
            placement_candidate.is_absolute()
            or metrics_candidate.is_absolute()
            or ".." in placement_candidate.parts
            or ".." in metrics_candidate.parts
        ):
            return None
        placement = output_dir / placement_candidate
        metrics = output_dir / metrics_candidate
        if not placement.is_file() or not metrics.is_file():
            return None
        if checkpoint.get("placement_sha256") != _sha256(placement):
            return None
        if checkpoint.get("metrics_sha256") != _sha256(metrics):
            return None
        validate_openparf_native_metrics(
            metrics, allow_detailed_legalization_handoff=True,
        )
        return placement.resolve()
    except (OSError, TypeError, ValueError, ValidationError):
        return None


def _write_openparf_result_checkpoint(
    output_dir: Path, placement: Path,
) -> Path:
    metrics = native_metrics_path(placement)
    if not placement.is_file() or not metrics.is_file():
        raise ValidationError("OpenPARF result checkpoint requires placement and metrics")
    try:
        placement_relative = placement.resolve().relative_to(output_dir.resolve())
        metrics_relative = metrics.resolve().relative_to(output_dir.resolve())
    except ValueError as error:
        raise ValidationError("OpenPARF result is outside its qualification root") from error
    checkpoint_path = output_dir / "openparf-result-checkpoint.json"
    write_json(checkpoint_path, {
        "schema": OPENPARF_RESULT_CHECKPOINT_SCHEMA,
        "input_sha256": _openparf_input_fingerprints(output_dir),
        "placement": str(placement_relative),
        "placement_sha256": _sha256(placement),
        "metrics": str(metrics_relative),
        "metrics_sha256": _sha256(metrics),
    }, compact=True)
    return checkpoint_path


def build_xilinx_openparf_atomic_source(mapped_path: Path, output_path: Path, *, top=None, mapped_value=None, mapped_sha256=None):
    from emuflow.physical.openparf.input_native import build_atomic_source
    return build_atomic_source(mapped_path, output_path, top=top, mapped_value=mapped_value, mapped_sha256=mapped_sha256)


def export_xilinx_openparf_atomic(mapped_path: Path, packed_path: Path, architecture_path: Path, output_dir: Path, *, top=None, native_constraints_path=None, provider_manifest_path=None, mapped_value=None, packed_value=None, architecture=None, allowed_slrs=None):
    from emuflow.physical.openparf.input_native import prepare_atomic_input
    return prepare_atomic_input(mapped_path, packed_path, architecture_path, output_dir, top=top, native_constraints_path=native_constraints_path, provider_manifest_path=provider_manifest_path, mapped_value=mapped_value, packed_value=packed_value, architecture=architecture, allowed_slrs=allowed_slrs)


def run_xilinx_openparf_atomic_qualification(
    mapped_path, packed_path, architecture_path, output_dir, *, top=None,
    openparf_install=None, openparf_python=None, native_constraints_path=None,
    provider_manifest_path=None, mapped_value=None, packed_value=None,
    architecture=None, allowed_slrs=None, constraints_path=None,
):
    runtime=validate_openparf_runtime(install_root=openparf_install,python_executable=openparf_python)
    manifest=export_xilinx_openparf_atomic(mapped_path,packed_path,architecture_path,output_dir,top=top,
        native_constraints_path=native_constraints_path,provider_manifest_path=provider_manifest_path,
        mapped_value=mapped_value,packed_value=packed_value,architecture=architecture,allowed_slrs=allowed_slrs)
    raw=_reuse_openparf_result(output_dir)
    reused=raw is not None
    if raw is None:
        raw=run_openparf(output_dir/'openparf.json',log_path=output_dir/'openparf.log',
            install_root=openparf_install,python_executable=openparf_python,
            allow_detailed_legalization_handoff=True)
    convergence=native_metrics_path(raw)
    metadata={}
    if convergence.is_file():
        metadata['native_convergence']={'artifact':{'path':str(convergence),'bytes':convergence.stat().st_size,
            'sha256':_sha256(convergence)},'metrics':validate_openparf_native_metrics(convergence,
                allow_detailed_legalization_handoff=True)}
    installation=Path(str(runtime.get('installation','')));python=Path(str(runtime.get('python','')))
    if (installation/'openparf.py').is_file() and (installation/'openparf').is_dir() and python.is_file():
        metadata['runtime_validation']='native-openparf'
    # This checkpoint owns the immutable solver output and its matching metrics.
    # Native legalization publishes a separate generation and never rewrites it.
    checkpoint=_write_openparf_result_checkpoint(output_dir,raw)
    imported=import_placement(raw,output_dir/'name_map.json',mapped_path,architecture_path,packed_path,
        output_dir/'placement-import-report.json',native_constraints_path=native_constraints_path,
        provider_manifest_path=provider_manifest_path,constraints_path=constraints_path,metadata=metadata,top=top)
    return {'status':'pass','runtime':runtime,'manifest':manifest,'placement':imported['placement'],
        'openparf_result':{'reused':reused,'checkpoint':str(checkpoint)},
        'certificate':imported['certificate'],'certificate_path':imported['certificate_path'],
        'bridge':imported['bridge'],'handoff_generation':imported['handoff_generation'],'native_import':{'implementation_id':imported['implementation_id'],
            'metrics':imported['native_metrics']},
        'qualification_scope':'native mixed-resource placement with exact MUX and CARRY8/LUT6_2 site macros, '
            'CARRY_NEXT adjacency, and typed DSP/BRAM/URAM legalization plus the rectangular multi-SLR SLL objective; '
            'clock and half-column constraints remain fail-closed'}
