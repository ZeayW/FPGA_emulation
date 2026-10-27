"""Prove a complete mapped source survived native physical-port resizing."""
import json
import os
from pathlib import Path
import tempfile

from .errors import ValidationError
from .native_protocol import CheckedRequestWriter
from .native_tools import resolve_native_executable
from .store_publication import (bind_store_source, require_store_sources_unchanged,
                                validate_store_destination)
from .tool_runtime import NativeToolProcess


def validate_mapped_normalization(original, normalized, primitive_source, output, *, executable=None):
    """The native core compares every cell, parameter and connection.

    This is deliberately narrower than synthesis equivalence: unchanged cell
    identities and parameters are required. The primitive source itself owns
    physical widths, rather than a caller-supplied list of pins to discard.
    """
    parents = tuple(bind_store_source(Path(path), schema) for path, schema in (
        (original, 'emuir-compact-v2'), (normalized, 'emuir-compact-v2'),
        (primitive_source, 'yosys-source-document-v1')))
    output = Path(output).resolve()
    validate_store_destination(output, parents)
    output.parent.mkdir(parents=True, exist_ok=True)
    native = resolve_native_executable('emuflow-native', executable)
    with tempfile.TemporaryDirectory(prefix='.mapped-normalization-', dir=output.parent) as temporary:
        root = Path(temporary)
        request, result = root/'request.bin', root/'proof.json'
        writer = CheckedRequestWriter(request)
        try:
            writer.put('QI', 0x31514e4d464d5545, 1)
            for parent in parents:
                writer.put('3Q4q', *parent.generation)
            writer.finish()
        finally:
            writer.close()
        with NativeToolProcess(native, [native, 'phase1', 'validate-mapped-normalization',
                *(str(parent.path) for parent in parents), str(request), str(result)],
                environment=dict(os.environ), timeout_ms=21600000,
                scratch_parent=root, maximum_bytes=65536) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError('mapped normalization proof failed: '+completed.stdout)
        if result.stat().st_size > 32768:
            raise ValidationError('mapped normalization proof exceeds bound')
        report = json.loads(result.read_text())
        if report.get('schema') != 'emuflow.mapped-normalization-proof/v1' or report.get('status') != 'pass':
            raise ValidationError('mapped normalization proof is invalid')
        require_store_sources_unchanged(parents)
        os.replace(result, output)
    return report
