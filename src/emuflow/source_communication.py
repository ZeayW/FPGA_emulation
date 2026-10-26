"""Bounded control for complete original-connectivity communication bounds.

The shared native producer joins original endpoints, assignment ownership and
scheduled route trees. No path selection, route graph or endpoint population
is materialized in Python. These planning bounds do not qualify post-route
timing or an operating frequency.
"""
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile

from .errors import EmuFlowError, ValidationError
from .native_protocol import CheckedRequestWriter
from .native_tools import resolve_native_executable
from .store_publication import (VerifiedStoreFile, require_store_sources_unchanged,
                                validate_store_destination)


def export_source_communication(original_ir, assignment, schedule, output, *, design,
        platform, semantic_sha256, fixed_delay_ns, serial_ui_ns, source_bindings,
        maximum_ratio=256, executable=None):
    """Export one source data-wire bound per actual remote sink pin.

    Callers retain their already validated parent generations. Each physical
    route hop contributes fixed_delay_ns + serial_ui_ns * its assigned ratio.
    Relay hops accumulate along the unique source-to-sink tree branch. The
    generated costs can drive OpenSTA's communication re-search primitive;
    they are not the schedule-resolved, physically measured final delays.
    """
    if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
            for v in (fixed_delay_ns, serial_ui_ns)) or fixed_delay_ns < 0 or serial_ui_ns <= 0
            or isinstance(maximum_ratio, bool) or not isinstance(maximum_ratio, int)
            or not 8 <= maximum_ratio <= 256 or maximum_ratio % 8
            or not all(isinstance(s, str) and s and '\0' not in s for s in (design, platform))
            or not isinstance(semantic_sha256, str) or re.fullmatch('[0-9a-f]{64}', semantic_sha256) is None):
        raise ValidationError('source communication model or source identity is invalid')
    paths = [Path(p).resolve() for p in (original_ir, assignment, schedule)]
    bindings = tuple(source_bindings)
    by_path = {binding.path: binding for binding in bindings}
    schemas = ('emuir-compact-v2', 'assignment-index-v4', 'tdm-schedule-store-v4')
    if (len(bindings) != 3 or len(by_path) != 3 or set(by_path) != set(paths)
            or any(by_path[path].schema != schema for path, schema in zip(paths, schemas))):
        raise ValidationError('source communication parent generation coverage differs')
    output = Path(output).resolve()
    validate_store_destination(output, bindings)
    native = resolve_native_executable('emuflow-native', executable)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.source-communication-', dir=output.parent) as directory:
        request = Path(directory) / 'request.bin'
        writer = CheckedRequestWriter(request)
        try:
            writer.put('QI', 0x31514353434d5545, 1)
            for path in paths:
                writer.put('QQQqqqq', *by_path[path].generation)
            writer.put('ddII', fixed_delay_ns, serial_ui_ns, 8, maximum_ratio)
            for name in (design, platform, semantic_sha256):
                writer.text(name)
            writer.finish()
        except BaseException:
            writer.close()
            raise
        result = subprocess.run([native, 'phase7', 'export-source-communication',
            *(str(path) for path in paths), str(request), str(output)], capture_output=True, text=True)
        if result.returncode:
            raise EmuFlowError('native source communication export failed: ' + result.stderr[-16384:])
        require_store_sources_unchanged(bindings)
        try:
            summary = json.loads(result.stdout)
        except (ValueError, TypeError) as error:
            raise EmuFlowError('source communication export lacks its terminal summary') from error
        if (not isinstance(summary, dict) or summary.get('schema') != 'emuflow.source-communication-export/v1'
                or summary.get('status') != 'pass'):
            raise EmuFlowError('source communication export lacks its terminal summary')
    seal = VerifiedStoreFile(output, 'source-communication-tsv-v1')
    return {**summary, 'output': str(output), 'sha256': seal.sha256, 'bytes': seal.bytes}
