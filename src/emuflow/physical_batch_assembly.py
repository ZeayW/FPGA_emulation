"""Join completed Vivado workers inside one active experiment."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping, Sequence

from .errors import ValidationError
from .io import file_sha256, read_json
from .multi_fpga_physical_flow import _assemble_multi_fpga_physical_results, _native_word_physical_sources
from .netlist import SPLIT_MANIFEST_SCHEMA
from .physical_backend import physical_backend_descriptor
from .platform import Platform
from .runtime import build_virtual_runtime
from .tdm_schedule_storage import canonical_tdm_schedule_sha256, read_tdm_schedule
from .transport_storage import read_virtual_binding
from .virtual_board import build_virtual_io_physical_contract, validate_virtual_board
from .vivado_clocks import implementation_timing_contract, validate_dut_frequency_mhz


def _canonical_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def assemble_vivado_physical_batches(
    split_root: Path, platform_path: Path, schedule_path: Path,
    batch_paths: Sequence[Path], output_dir: Path,
) -> dict:
    """Validate source-bound worker records, then publish all-FPGA results.

    This consumes scratch from the same active experiment. It never reruns
    synthesis or implementation, and does not run or qualify global Phase 7C.
    Complete local and cross-FPGA timing evidence remains mandatory.
    """
    split_root = split_root.resolve()
    manifest_path = split_root / "manifest.json"
    manifest = read_json(manifest_path)
    if manifest.get("schema") != SPLIT_MANIFEST_SCHEMA:
        raise ValidationError("physical batch split manifest is invalid")
    platform = Platform.load(platform_path)
    expected = [fpga.id for fpga in platform.fpgas]
    if not batch_paths or len(batch_paths) > len(expected):
        raise ValidationError("physical batch coverage is empty or duplicated")
    if output_dir.exists() and (not output_dir.is_dir() or any(output_dir.iterdir())):
        raise ValidationError("physical assembly requires an empty output directory")
    clock_periods = manifest.get("clock_periods")
    if not isinstance(clock_periods, dict) or not clock_periods:
        raise ValidationError("physical batch lacks Phase 6 clock periods")
    schedule = read_tdm_schedule(schedule_path)
    _native_word_physical_sources(split_root, manifest, platform, schedule, 'vivado')
    runtime = build_virtual_runtime(schedule, platform, clock_periods)
    io_sources = manifest.get("virtual_board_binding")
    binding = None
    if io_sources is not None:
        if (not isinstance(io_sources, Mapping)
                or any(not isinstance(io_sources.get(name), str) for name in ("wiring", "binding"))
                or any(not isinstance(io_sources.get(name), Mapping)
                       or set(io_sources[name]) != set(expected)
                       for name in ("xdc", "differential_io"))):
            raise ValidationError("physical batch fixed I/O source coverage is invalid")
        board = read_json(split_root / io_sources["wiring"])
        validate_virtual_board(platform, board)
        binding = read_virtual_binding(split_root / io_sources["binding"])
        if (binding.get("board_sha256") != _canonical_sha(board)
                or binding.get("schedule_sha256") != canonical_tdm_schedule_sha256(schedule)):
            raise ValidationError("physical batch fixed I/O source seals differ")
    source = {name: file_sha256(path) for name, path in (
        ("split_manifest", manifest_path), ("platform", platform_path),
        ("schedule", schedule_path),
    )}
    backend = physical_backend_descriptor("vivado")
    architecture = {"status": "pass", "mode": "platform-parts",
                    "parts": sorted({fpga.part for fpga in platform.fpgas}),
                    "provider": "vivado-device-database"}
    records, seen = [], set()
    execution_contract = None
    for path in batch_paths:
        batch = read_json(path)
        if (not isinstance(batch, Mapping)
                or batch.get("schema") != "emuflow.physical-partition-batch/v1"
                or batch.get("status") != "pass" or batch.get("backend") != backend
                or batch.get("architecture") != architecture
                or batch.get("expected_fpgas") != expected):
            raise ValidationError("physical batch backend, device or coverage differs")
        bindings = batch.get("source")
        if (not isinstance(bindings, Mapping)
                or any(not isinstance(bindings.get(name), Mapping)
                       or bindings[name].get("sha256") != digest
                       for name, digest in source.items())):
            raise ValidationError("physical batch belongs to different source inputs")
        execution = batch.get("execution", {})
        if not isinstance(execution, Mapping):
            raise ValidationError("physical batch execution contract is invalid")
        options = execution.get("vivado_implementation")
        seed = execution.get("seed")
        if (not isinstance(seed, int) or isinstance(seed, bool) or seed < 1
                or not isinstance(options, dict)
                or set(options) not in ({"place_directive", "route_directive", "dut_frequency_mhz"},
                    {"place_directive", "route_directive", "dut_frequency_mhz", "dut_timing_io_sha256"})
                or ('dut_timing_io_sha256' in options and (not isinstance(options['dut_timing_io_sha256'],str)
                    or len(options['dut_timing_io_sha256'])!=64 or any(c not in '0123456789abcdef'for c in options['dut_timing_io_sha256'])))
                or any(not isinstance(options[name], str) or not options[name]
                       for name in ("place_directive", "route_directive"))):
            raise ValidationError("physical batch implementation options are missing")
        validate_dut_frequency_mhz(options["dut_frequency_mhz"])
        contract = {"seed": seed, "vivado_implementation": options}
        if execution_contract is not None and contract != execution_contract:
            raise ValidationError("physical batch implementation options differ")
        execution_contract = contract
        selected, references = batch.get("selected_fpgas"), batch.get("records")
        if (not isinstance(selected, list) or not selected
                or any(not isinstance(fpga, str) for fpga in selected)
                or len(set(selected)) != len(selected) or set(selected) - set(expected)
                or not isinstance(references, list) or len(references) != len(selected)):
            raise ValidationError("physical batch selected coverage is invalid")
        batch_seen = set()
        for reference in references:
            if not isinstance(reference, dict):
                raise ValidationError("physical batch record reference is invalid")
            fpga = reference.get("fpga")
            if fpga not in selected or fpga in seen:
                raise ValidationError("physical batch FPGA is duplicated or unselected")
            if not isinstance(reference.get("path"), str) or not reference["path"]:
                raise ValidationError("physical batch record path is invalid")
            record_path = Path(reference["path"])
            if not record_path.is_absolute():
                record_path = path.parent / record_path
            if file_sha256(record_path) != reference.get("sha256"):
                raise ValidationError("physical batch record digest differs")
            record = read_json(record_path)
            if (not isinstance(record, Mapping) or record.get("fpga") != fpga
                    or record.get("status") != "pass"):
                raise ValidationError("physical batch record identity or status differs")
            if record.get("active") is not False:
                stages = record.get("stages", {})
                if not isinstance(stages, Mapping):
                    raise ValidationError("physical batch implementation stages are invalid")
                native = stages.get("vivado_implementation", {})
                implemented_options = native.get('implementation_options') if isinstance(native, Mapping) else None
                if (not isinstance(implemented_options, Mapping) or
                        ('retain_routed_checkpoint' in implemented_options and not isinstance(implemented_options['retain_routed_checkpoint'],bool)) or
                        {k:v for k,v in implemented_options.items()if k!='retain_routed_checkpoint'} != options):
                    raise ValidationError("physical batch declared and implemented directives differ")
                if native.get("implementation_timing") != implementation_timing_contract(runtime, options["dut_frequency_mhz"]):
                    raise ValidationError("physical batch implementation target-clock contract differs")
                for name in ("boundary_timing", "logic_segment_timing", "local_path_timing"):
                    if not isinstance(native.get(name), Mapping) or native[name].get("status") != "pass":
                        raise ValidationError("physical batch timing coverage is incomplete")
                budget = next(item.to_dict() for item in platform.fpgas if item.id == fpga)
                digest = _canonical_sha({**runtime, "resource_budget": budget})
                identity = native.get("input_identity")
                if not isinstance(identity, Mapping) or identity.get("runtime_sha256") != digest:
                    raise ValidationError("physical batch runtime or resource budget differs")
                if binding is not None:
                    io_contract = build_virtual_io_physical_contract(
                        binding, fpga, xdc_path=split_root / io_sources["xdc"][fpga],
                        adapter_path=split_root / io_sources["differential_io"][fpga])
                    evidence = stages.get("virtual_io_implementation")
                    if (not isinstance(evidence, Mapping)
                            or evidence.get("contract_sha256") != _canonical_sha(io_contract)):
                        raise ValidationError("physical batch implemented fixed I/O differs from Phase 6")
            seen.add(fpga)
            batch_seen.add(fpga)
            records.append(record)
        if batch_seen != set(selected):
            raise ValidationError("physical batch selected coverage is incomplete")
    if seen != set(expected):
        raise ValidationError("physical batch all-FPGA coverage is incomplete")
    return _assemble_multi_fpga_physical_results(
        records=records, manifest=manifest, manifest_path=manifest_path,
        platform=platform, runtime=runtime, split_root=split_root,
        output_dir=output_dir, backend_descriptor=backend,
        architecture_source=architecture, include_logic_timing=True,
        execution={**execution_contract, "ordering": "boarddb-fpga-order",
                   "mode": "completed-active-run-workers",
                   "worker_batches": len(batch_paths)},
    )
