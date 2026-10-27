"""Bounded source leases for the native physical logic-query owner."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Mapping

from emuflow.common.errors import ValidationError
from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable
from emuflow.storage.store_publication import StoreSource, bind_store_source, require_store_sources_unchanged
from emuflow.common.tool_runtime import NativeToolProcess
from emuflow.physical.xilinx_primitives import DEFAULT_XILINX_PRIMITIVE_LIBRARY
from emuflow.timing.local_path_query_native import _small, _payload

_FIELDS = ('device', 'inode', 'bytes', 'mtime_seconds', 'mtime_nanoseconds',
           'ctime_seconds', 'ctime_nanoseconds')


@dataclass(frozen=True)
class LogicSegmentQueryInputs:
    """Original typed owners and bounded configuration; contains no graph rows."""
    original_ir_path: Path
    assignment_path: Path
    path_database_path: Path
    routes_path: Path
    schedule_path: Path
    platform: Any
    sources: tuple[StoreSource, ...]
    selectors: tuple[StoreSource, ...]
    assignment: Mapping[str, Any]
    request: Mapping[str, Any]
    _lifetime: Any

    @property
    def original_ir(self):
        return None

    def close(self):
        self._lifetime.cleanup()


def prepare_logic_segment_query_inputs(original_ir_path, assignment_path,
        path_database_path, routes_path, schedule_path, platform):
    from emuflow.partition.phase3_assignment_index import open_phase3_assignment_index, _exact_binary_reference
    from emuflow.routing.system_route_storage import read_system_routes, SystemRoutesView
    from emuflow.scheduling.tdm_schedule_storage import TDMScheduleView
    from emuflow.routing.routing import normalize_route_constraints, build_directed_graph
    from emuflow.scheduling.tdm import _bank_direction_allocations, _write_native_schedule_board_request

    original, assignment, paths, routes, schedule = (Path(p).resolve() for p in (
        original_ir_path, assignment_path, path_database_path, routes_path, schedule_path))
    selectors = tuple(bind_store_source(p, role) for p, role in (
        (assignment, 'physical-input-document-v1'), (paths, 'sta-path-selector-v1'),
        (routes, 'system-route-selector-v1'), (schedule, 'tdm-schedule-selector-v4')))
    if assignment.suffix == '.sqlite':
        assignment_store = assignment
    else:
        value = _small(assignment)
        if value.get('schema') != 'emuflow.phase3-assignment-storage/v4':
            raise ValidationError('native logic queries require assignment v4')
        assignment_store = _payload(assignment, value.get('cut_nets_index', {}), 'artifact')
    with closing(open_phase3_assignment_index(assignment_store)) as db:
        if db.execute('PRAGMA user_version').fetchone()[0] != 4:
            raise ValidationError('native logic queries require assignment v4')
        metadata = {}
        for key in ('design', 'platform', 'constraints', 'semantic_contract_sha256'):
            row = db.execute('SELECT CASE WHEN length(value)<=8388608 THEN value END FROM metadata WHERE key=?', (key,)).fetchone()
            if row is None or row[0] is None:
                raise ValidationError('logic query assignment metadata is missing or oversized')
            metadata[key] = json.loads(row[0])
        exact, _ = _exact_binary_reference(db, assignment_store)
    paths_manifest = _small(paths)
    payload = paths_manifest.get('payloads', {}).get('paths', {})
    if paths_manifest.get('schema') != 'emuflow.sta-path-database/v3' or payload.get('format') != 'sqlite-sta-path/v1':
        raise ValidationError('native logic queries require the current typed timing-path owner')
    path_store = _payload(paths, payload, 'path')
    route_view = read_system_routes(routes)
    if not isinstance(route_view, SystemRoutesView) or route_view.storage_manifest.get('user_version') != 3:
        raise ValidationError('native logic queries require RouteStore v3')
    schedule_view = TDMScheduleView(schedule)
    lifetime = tempfile.TemporaryDirectory(prefix='emuflow-native-logic-query-')
    try:
        root = Path(lifetime.name)
        constraints = normalize_route_constraints(route_view.get('constraints'), platform)
        _, arcs, capacities = build_directed_graph(platform, constraints)
        allocations = _bank_direction_allocations(route_view, platform, constraints)
        board = root/'board.bin'
        _write_native_schedule_board_request(board, native_file_generation(route_view.store_path), platform,
            constraints, arcs, allocations, sorted(f.id for f in platform.fpgas),
            sorted(link.id for link in platform.links), sorted(capacities))
        # Only source headers, counts and digests cross this boundary.
        sources = tuple(bind_store_source(p, role) for p, role in (
            (original, 'emuir-compact-v2'), (assignment_store, 'assignment-index-v4'),
            (exact, 'phase3-exact-data-v2'), (route_view.store_path, 'system-route-store-v3'),
            (schedule_view.store_path, 'tdm-schedule-store-v4'), (path_store, 'timing-path-store-v1')))
        request = {'schema': 'emuflow.logic-segment-query/v1', 'design': metadata['design'],
            'platform': platform.name, 'route_sha256': route_view.storage_manifest['logical_sha256'],
            'route_file_sha256': route_view.storage_manifest['store_sha256'],
            'schedule_sha256': schedule_view.manifest['logical_sha256'],
            'schedule_pretty_sha256': schedule_view.manifest['logical_pretty_sha256'],
            'schedule_sections': schedule_view.manifest['sections'], 'path_sha256': payload['sha256']}
        sources += (bind_store_source(board, 'exact-schedule-board-request-v1'),)
        require_store_sources_unchanged((*sources, *selectors))
        return LogicSegmentQueryInputs(original, assignment, paths, routes, schedule, platform,
            sources, selectors, metadata, request, lifetime)
    except BaseException:
        lifetime.cleanup()
        raise
    finally:
        schedule_view.close()


def _write_inputs(prepared, merged, boundary, fpga, provider, query_name, identity_name, root):
    from emuflow.timing.boundary_identity_storage import BoundaryIdentityView
    with BoundaryIdentityView(boundary) as view:
        boundary_sources = view.bound_store_sources()
        boundary_store = bind_store_source(view.store_path, 'boundary-identity-store-v1')
    # Fixed native role order: original/assignment/exact/routes/schedule/paths,
    # merged/boundary/board/library/control request.
    sources = (*prepared.sources[:6], bind_store_source(merged, 'emuir-compact-v2'),
        boundary_store, prepared.sources[6],
        bind_store_source(DEFAULT_XILINX_PRIMITIVE_LIBRARY, 'xilinx-primitive-library-v1'))
    config = dict(prepared.request, fpga=fpga, provider=provider, query_name=query_name,
        identity_name=identity_name, report_name='logic-query-report.json')
    path = root/'request.bin'
    writer = CheckedRequestWriter(path)
    try:
        writer.put('QI', 0x31514C50464D5545, 1)
        for source in sources:
            writer.put('3Q4q', *source.generation)
        writer.text(json.dumps(config, ensure_ascii=False, separators=(',', ':'), sort_keys=True))
        writer.finish()
    finally:
        writer.close()
    return (*sources, bind_store_source(path, 'logic-segment-query-request-v1')), boundary_sources


def write_logic_segment_query(original_ir_path, assignment_path, path_database_path,
        routes_path, schedule_path, platform, merged_ir_path, boundary_identity_path,
        fpga, query_path, identity_path, *, object_provider, prepared_inputs=None):
    owned = prepared_inputs is None
    prepared = prepared_inputs or prepare_logic_segment_query_inputs(original_ir_path,
        assignment_path, path_database_path, routes_path, schedule_path, platform)
    expected = tuple(Path(p).resolve() for p in (original_ir_path, assignment_path,
        path_database_path, routes_path, schedule_path))
    if (not isinstance(prepared, LogicSegmentQueryInputs) or expected != (
            prepared.original_ir_path, prepared.assignment_path, prepared.path_database_path,
            prepared.routes_path, prepared.schedule_path) or platform != prepared.platform):
        raise ValidationError('logic query preparation belongs to another original run')
    if object_provider not in {'xilinx', 'vivado'} or not isinstance(fpga, str) or not fpga:
        raise ValidationError('logic query FPGA/backend is invalid')
    require_native_schema('logic-segment-query-request-v1')
    parent = Path(identity_path).resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
    generation = None
    try:
        with tempfile.TemporaryDirectory(prefix='.logic-query-host-', dir=parent) as temporary:
            root = Path(temporary)
            sources, boundary_sources = _write_inputs(prepared, Path(merged_ir_path).resolve(),
                Path(boundary_identity_path).resolve(), fpga, object_provider,
                Path(query_path).name, Path(identity_path).name, root)
            leases = (*sources, *prepared.selectors, *boundary_sources)
            for output in (Path(query_path).resolve(), Path(identity_path).resolve()):
                if any(output == source.path or (output.exists() and os.path.samefile(output, source.path)) for source in leases):
                    raise ValidationError('logic query destination aliases an original source')
            generation = Path(tempfile.mkdtemp(prefix=Path(identity_path).name+'.generation.', dir=parent))
            generation.rmdir()
            proof = root/'operation.json'
            executable = resolve_native_executable('emuflow-native')
            command = [executable, '--report', str(proof), 'physical', 'query-logic-segments',
                *(str(source.path) for source in sources), str(generation)]
            require_store_sources_unchanged(leases)
            with NativeToolProcess(executable, command, environment=dict(os.environ), scratch_parent=root) as process:
                result = process.finish()
            if result.returncode:
                raise ValidationError('native logic query owner failed: '+result.stdout)
            evidence = _small(proof, 262144)
            rows = evidence.get('outputs', [])
            if evidence.get('stage') != 'physical.query-logic-segments' or evidence.get('status') != 'pass' or len(rows) != 4:
                raise ValidationError('native logic query publication proof disagrees')
            expected_roles = ('logic-segment-query-tsv-v1', 'logic-segment-identity-selector-v1',
                'logic-segment-identity-store-v1', 'logic-segment-query-summary-v1')
            outputs = []
            for row, role in zip(rows, expected_roles):
                path = Path(row['path'])
                observed = tuple(row.get('generation', {}).get(field) for field in _FIELDS)
                if (row.get('schema') != role or path.parent != generation or path.is_symlink() or
                        any(type(n) is not int for n in observed) or observed != native_file_generation(path)):
                    raise ValidationError('native logic query output generation changed')
                outputs.append(StoreSource(path, role, observed))
            report = _small(outputs[3].path, 262144)
            if (report.get('status') != 'pass' or report.get('implementation_id') != 'native-logic-segment-query-v1' or
                    report.get('query') != str(outputs[0].path) or report.get('identity') != str(outputs[1].path)):
                raise ValidationError('native logic query summary disagrees')
            require_store_sources_unchanged((*leases, *outputs))
            return report
    except BaseException:
        if generation is not None:
            shutil.rmtree(generation, ignore_errors=True)
        raise
    finally:
        if owned:
            prepared.close()
