"""Bounded configuration bridge for the complete native Phase 5 stage."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile

from .errors import TDMScheduleInfeasibleError, ValidationError
from .native_protocol import CheckedRequestWriter
from .native_tools import resolve_native_executable
from .store_publication import require_store_sources_unchanged
from .tool_runtime import NativeToolProcess
from .tdm_schedule_storage import write_native_exact_generation_request


def _write_inputs(producer, root):
    """All collections here are board/configuration sized, never hop records."""
    sources = producer.source_bindings
    require_store_sources_unchanged(sources)
    board, generation, request = (root / name for name in ("board.bin", "generation.bin", "stage.bin"))
    producer.write_board_request(board)
    write_native_exact_generation_request(generation, producer)
    writer = CheckedRequestWriter(request)
    try:
        writer.put("QII", 0x31535035464D5545, 1, len(sources))
        for source in sources:
            writer.text(str(source.path))
            writer.text(source.schema)
            writer.put("3Q4q", *source.generation)
        writer.finish()
    finally:
        writer.close()
    require_store_sources_unchanged(sources)
    return [str(producer.route_store), str(board), str(generation), str(request)]


def run_native_phase5(producer, output_dir):
    from .phase5 import validate_phase5_generation

    output_dir = Path(output_dir).absolute()
    output_dir.mkdir(parents=True, exist_ok=True)
    executable = resolve_native_executable("emuflow-native")
    with tempfile.TemporaryDirectory(prefix=".phase5-request-", dir=output_dir.parent) as temporary:
        root = Path(temporary)
        inputs = _write_inputs(producer, root)
        report = output_dir / "phase5_report.json"
        with NativeToolProcess(
            executable, [executable, "phase5", "run", *inputs, str(report)],
            environment=dict(os.environ), timeout_ms=21600000, scratch_parent=root,
        ) as process:
            result = process.finish()
        if result.returncode:
            if "infeasible" in result.stdout:
                raise TDMScheduleInfeasibleError(result.stdout)
            raise ValidationError("native Phase 5 failed: " + result.stdout)
        require_store_sources_unchanged(producer.source_bindings)
        if report.stat().st_size > 256 * 1024:
            raise ValidationError("native Phase 5 report exceeds bound")
        return validate_phase5_generation(report, managed_root_sealed=True)["report"]
