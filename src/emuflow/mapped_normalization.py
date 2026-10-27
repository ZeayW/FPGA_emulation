"""Prove a complete mapped source survived native physical-port resizing."""
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
import tempfile

from .errors import ValidationError
from .io import file_sha256, write_json
from .native_protocol import CheckedRequestWriter
from .native_tools import resolve_native_executable
from .store_publication import (StoreSource, bind_store_source, require_store_sources_unchanged,
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
        # The native comparison proves connectivity. Bind that result to the
        # exact input generations and content seals before another stage uses
        # it to join normalized timing with an existing physical partition.
        report['source_artifacts'] = {
            role: {'path':str(parent.path), 'schema':parent.schema,
                'generation':list(parent.generation), 'bytes':parent.generation[2],
                'sha256':file_sha256(parent.path)}
            for role,parent in zip(('original','normalized','primitive_source'), parents)
        }
        if report['source_artifacts']['primitive_source']['sha256'] != report.get('primitive_source_sha256'):
            raise ValidationError('mapped normalization primitive source seal differs')
        require_store_sources_unchanged(parents)
        write_json(result, report, compact=True)
        if result.stat().st_size > 32768:
            raise ValidationError('source-bound mapped normalization proof exceeds bound')
        os.replace(result, output)
    return report


@dataclass(frozen=True)
class MappedNormalizationProof:
    """An immutable admission owner; counts alone cannot bridge source views."""
    artifact: StoreSource
    artifact_sha256: str
    parents: tuple[StoreSource, ...]
    source_sha256: tuple[str, str, str]
    cells: int
    retained_pins: int
    removed_nonphysical_pins: int

    def require_unchanged(self):
        require_store_sources_unchanged((self.artifact, *self.parents))


def load_mapped_normalization_proof(path, *, original, normalized, primitive_source,
                                    original_sha256, normalized_sha256, primitive_source_sha256):
    """Check the supplied parent seals without reloading or rehashing a graph.

    The seals belong to the caller's already validated IR/library inputs. File
    generations connect those seals to the comparison's original invocation.
    An older counts-only report does not establish that connection.
    """
    artifact = bind_store_source(Path(path), 'mapped-normalization-proof-v1')
    if artifact.generation[2] > 32768:
        raise ValidationError('source-bound mapped normalization proof exceeds bound')
    try:
        report = json.loads(artifact.path.read_text())
    except (ValueError, UnicodeError) as exc:
        raise ValidationError('invalid source-bound mapped normalization proof') from exc
    if (not isinstance(report, dict) or report.get('schema') != 'emuflow.mapped-normalization-proof/v1'
            or report.get('status') != 'pass' or not isinstance(report.get('source_artifacts'), dict)
            or set(report['source_artifacts']) != {'original','normalized','primitive_source'}):
        raise ValidationError('mapped normalization admission requires a source-bound proof')
    for key in ('cells', 'retained_pins', 'removed_nonphysical_pins'):
        if type(report.get(key)) is not int or report[key] < 0:
            raise ValidationError('mapped normalization proof counts are invalid')
    parents = tuple(bind_store_source(Path(value), schema) for value,schema in (
        (original,'emuir-compact-v2'), (normalized,'emuir-compact-v2'),
        (primitive_source,'yosys-source-document-v1')))
    seals = (original_sha256, normalized_sha256, primitive_source_sha256)
    for role,parent,seal in zip(('original','normalized','primitive_source'), parents, seals):
        record = report['source_artifacts'][role]
        if (not isinstance(seal, str) or re.fullmatch('[0-9a-f]{64}', seal) is None or
                not isinstance(record, dict) or record.get('sha256') != seal or
                record.get('path') != str(parent.path) or record.get('schema') != parent.schema or
                type(record.get('bytes')) is not int or record['bytes'] != parent.generation[2] or
                not isinstance(record.get('generation'), list) or
                any(type(value) is not int for value in record['generation']) or
                tuple(record['generation']) != parent.generation):
            raise ValidationError(f'mapped normalization {role} parent seal or generation differs')
    if report.get('primitive_source_sha256') != primitive_source_sha256:
        raise ValidationError('mapped normalization primitive source seal differs')
    owner = MappedNormalizationProof(artifact, file_sha256(artifact.path), parents, seals,
        report['cells'], report['retained_pins'], report['removed_nonphysical_pins'])
    owner.require_unchanged()
    return owner
