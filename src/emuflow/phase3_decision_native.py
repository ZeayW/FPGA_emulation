"""Original solver owners to independent resource/constraint/selection evidence."""
import json
import os
from dataclasses import dataclass
from pathlib import Path
import tempfile
from types import SimpleNamespace

from .errors import ValidationError
from .native_protocol import CheckedRequestWriter
from .native_tools import require_native_schema, resolve_native_executable
from .store_publication import (StoreSource,
                               require_store_sources_unchanged)
from .tool_runtime import NativeToolProcess


def _policy_snapshot(platform, constraints):
    return json.dumps({'constraints': constraints,
        'fpgas': [(fpga.id, fpga.effective_capacity) for fpga in platform.fpgas]},
        sort_keys=True, separators=(',', ':'), allow_nan=False)


@dataclass(frozen=True)
class _OwnershipAudit:
    facts: dict
    sources: tuple
    owner: object
    policy: str

    def require(self, platform, assignment):
        if self.owner is not assignment['cluster_assignment'] or self.policy != _policy_snapshot(platform, assignment['constraints']):
            raise ValidationError('native partition audit belongs to another ownership or policy')
        require_store_sources_unchanged(self.sources)
        return self.facts


def ownership_sources(records, assignment):
    from .phase3_semantics_native import _ownership_sources
    source = getattr(records, 'native_source_path', None)
    if source is None:
        return None
    owner = _ownership_sources(SimpleNamespace(path=source), records, assignment)
    return None if owner is not None and owner[0] == 0 else owner


def _run(platform, clusters, assignments):
    records = clusters['clusters']
    owners = [ownership_sources(records, value['cluster_assignment']) for value in assignments]
    if any(owner is None for owner in owners):
        return None  # Explicit reference/diagnostic inputs have no native owner.
    if len(owners) == 2 and [owner[0] for owner in owners] != [1, 2]:
        return None
    first = owners[0]
    if any(owner[1:3] != first[1:3] for owner in owners):
        raise ValidationError('native partition decision belongs to mixed primary sources')
    constraints = assignments[0]['constraints']
    if any(value['constraints'] != constraints for value in assignments):
        raise ValidationError('native partition decision has different constraints')
    sources = [first[1], first[2], *(item for owner in owners for item in owner[3])]
    kind = 3 if len(owners) == 2 else first[0]
    require_native_schema('partition-decision-request-v1')
    from .partition_limits import planning_resource_capacities
    from .resources import RESOURCE_FIELDS
    parts = tuple(fpga.id for fpga in platform.fpgas)
    planning = planning_resource_capacities(platform.fpgas, constraints.get('resource_reservations_by_fpga', {}))
    dimensions = ('cells', *RESOURCE_FIELDS)
    balanced = constraints.get('balance_dimensions')
    explicit = json.dumps({key: constraints.get(key, []) for key in ('groups', 'fixed')},
                          sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(explicit.encode()) > 8 * 1024 * 1024 - 65536:
        raise ValidationError('native partition fixed/group request exceeds bound')
    with tempfile.TemporaryDirectory(prefix='emuflow-partition-decision-') as temporary:
        root = Path(temporary)
        request, output, operation = root/'request.bin', root/'summary.json', root/'operation.json'
        writer = CheckedRequestWriter(request)
        try:
            writer.put('QII', 0x31514450464D5545, 1, kind)
            for source in sources:
                writer.put('3Q4q', *source.generation)
            for value in assignments:
                writer.catalog(value['cluster_assignment'].part_ids)
            writer.catalog(parts)
            writer.catalog(constraints.get('active_fpgas', parts))
            writer.put('Q', constraints.get('min_used_fpgas', 1))
            for field in dimensions:
                writer.put('Bd', int(balanced is None or field in balanced),
                           constraints.get('balance_tolerance_by_dimension', {}).get(field, constraints['balance_tolerance']))
            for fpga in platform.fpgas:
                for field in RESOURCE_FIELDS:
                    writer.put('QQ', fpga.effective_capacity.get(field, 0), planning[fpga.id].get(field, 0))
            writer.text(explicit)
            if kind == 3:
                for value in assignments:
                    writer.text(value['provider'])
            writer.finish()
        finally:
            writer.close()
        require_store_sources_unchanged(sources)
        tool = resolve_native_executable('emuflow-native')
        arguments = [str(sources[0].path), str(sources[1].path), str(request),
                     *(str(source.path) for source in sources[2:]), str(output)]
        with NativeToolProcess(tool, [tool, '--report', str(operation), 'phase3', 'audit-decision', *arguments],
                               environment=dict(os.environ), scratch_parent=root) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError('native partition decision failed: ' + completed.stdout)
        require_store_sources_unchanged(sources)
        if operation.stat().st_size > 256 * 1024:
            raise ValidationError('native partition decision operation exceeds bound')
        proof = json.loads(operation.read_text())
        outputs = proof.get('outputs')
        if proof.get('stage') != 'phase3.audit-decision' or proof.get('status') != 'pass' or not isinstance(outputs, list) or len(outputs) != 1:
            raise ValidationError('native partition decision producer result disagrees')
        record = outputs[0]
        fields = ('device', 'inode', 'bytes', 'mtime_seconds', 'mtime_nanoseconds', 'ctime_seconds', 'ctime_nanoseconds')
        generation = record.get('generation', {})
        if record.get('path') != str(output.resolve()) or record.get('schema') != 'partition-decision-summary-v1' or any(type(generation.get(key)) is not int for key in fields):
            raise ValidationError('native partition decision output binding disagrees')
        original = StoreSource(output.resolve(), 'partition-decision-summary-v1', tuple(generation[key] for key in fields))
        require_store_sources_unchanged((original,))
        if output.stat().st_size > 16384 + len(parts) * 4096:
            raise ValidationError('native partition decision summary exceeds bound')
        result = json.loads(output.read_text())
        require_store_sources_unchanged((*sources, original))
    if (result.get('schema') != 'emuflow.partition-decision/v1' or result.get('status') != 'pass'
            or result.get('implementation_id') != 'native-partition-decision-v1'
            or not isinstance(result.get('audits'), list) or len(result['audits']) != len(assignments)):
        raise ValidationError('native partition decision summary disagrees')
    for audit in result['audits']:
        if (audit.get('schema') != 'emuflow.partition-load-summary/v1' or audit.get('parts') != list(parts)
                or audit.get('clusters') != len(records) or audit.get('instances') != len(records.source_cluster_indices)):
            raise ValidationError('native partition decision population disagrees')
    snapshot = _policy_snapshot(platform, constraints)
    result['native_audits'] = tuple(_OwnershipAudit(audit, tuple(sources), value['cluster_assignment'], snapshot)
                                  for audit, value in zip(result['audits'], assignments))
    result['native_metrics'] = completed.native_metrics
    return result


def audit_assignment(platform, clusters, assignment):
    result = _run(platform, clusters, (assignment,))
    return None if result is None else result['native_audits'][0]


def select_assignment(platform, clusters, initial, candidate):
    result = _run(platform, clusters, (initial, candidate))
    if result is None:
        return None
    selection = result.get('selection', {})
    if selection.get('status') != 'pass' or selection.get('selected') not in {'initial', 'candidate'}:
        raise ValidationError('native partition candidate selection disagrees')
    return result
