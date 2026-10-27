"""Small factual inputs for the sole native Global STA implementation.

This module publishes existing records into current stores. It does not bind
paths, propagate events, compose delays, or reduce timing results.
"""
from contextlib import ExitStack
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile

from emuflow.global_sta import EventCheck, build_opensta_timing as _native_build_timing

_BASELINES = json.loads((Path(__file__).parent / 'data/global_sta_binding_records.json').read_text())
_COUNTS = {}


def bind_physical_checks(runtime, routes, schedule, physical, platform, *, metadata=None,
                         routes_artifact_sha256=None):
    from emuflow.global_sta import bind_physical_checks as bind
    from emuflow.system_route_storage import read_system_routes, pretty_system_routes_sha256
    from tests.native_route_seal_fixture import write_route_store_fixture
    from emuflow.tdm_schedule_storage import read_tdm_schedule
    from tests.schedule_store_fixture import write_tdm_schedule_storage
    from emuflow.boundary_identity_storage import write_boundary_identity_storage
    from emuflow.boundary_timing_storage import write_boundary_timing_storage
    from emuflow.logic_segment_timing_storage import write_logic_segment_timing_storage
    from emuflow.local_path_timing_storage import write_local_path_timing_storage
    with tempfile.TemporaryDirectory(prefix='global-sta-input-fixture-') as temporary, ExitStack() as stack:
        root = Path(temporary)
        if not getattr(routes['routes'], 'store_path', None):
            write_route_store_fixture(root / 'routes.json', routes)
            indexed_routes = read_system_routes(root / 'routes.json')
        else:
            indexed_routes = routes
        if not getattr(schedule, 'store_path', None):
            # The fixture publisher requires arrays; preserve the caller's
            # explicit records without running any scheduling implementation.
            value = dict(schedule)
            for key in ('entries', 'routes', 'route_completion'):
                if key in value:value[key] = list(value[key])
            write_tdm_schedule_storage(root / 'schedule.json', value)
            indexed_schedule = read_tdm_schedule(root / 'schedule.json')
        else:
            indexed_schedule = schedule
        entry_records = value['entries'] if not getattr(schedule, 'store_path', None) else schedule['entries']
        entries = {row['id']: row for row in entry_records}
        indexed = dict(physical)
        for key, writer, section in (
            ('boundary_identities', write_boundary_identity_storage, 'endpoints'),
            ('boundary_timing', write_boundary_timing_storage, 'endpoints'),
            ('logic_segment_timing', write_logic_segment_timing_storage, 'segments'),
            ('local_path_timing', write_local_path_timing_storage, 'paths'),
        ):
            indexed[key] = {}
            for fpga, database in physical.get(key, {}).items():
                rows = [dict(row) for row in database[section]]
                header = {k:v for k,v in database.items() if k not in {
                    section, 'unsupported_member_paths', 'unmeasured_segments',
                }}
                if key == 'boundary_identities':
                    for row in rows:
                        entry = entries[row['schedule_entry']]
                        kind = row['kind']
                        defaults = dict(demand=entry['demand'], logical_net=entry['net'], link=entry['link'],
                            peer=entry['to'] if kind=='tx' else entry['from'], lane=entry['lane'],
                            logical_lane=entry['lane'], internal_signal='net:'+entry['net'],
                            source_class='dut-net' if kind=='tx' else 'captured-shadow')
                        for field, value in defaults.items():row.setdefault(field, value)
                        row.setdefault('merged_ir', {})
                        merged = dict(row['merged_ir'])
                        for field, value in dict(external_port=kind, external_port_bit=row['lane'],
                            external_net=row['id'], logical_net=entry['net'],
                            boundary_register_instances=[] if kind=='tx' else ['rx']).items():
                            merged.setdefault(field, value)
                        row['merged_ir'] = merged
                if key == 'boundary_timing':
                    header.setdefault('measurement_scope', 'partition-boundary')
                if key == 'logic_segment_timing':
                    header.update(system_routes_sha256=pretty_system_routes_sha256(indexed_routes),
                                  tdm_schedule_sha256=indexed_schedule.manifest['logical_pretty_sha256'])
                    rows.sort(key=lambda r:(r['system_path'],r['member_path'],r['cut_index'],r['id']))
                    view = writer(root / f'{key}-{fpga}.json', header=header, segments=rows,
                        unsupported_member_paths=database.get('unsupported_member_paths', ()),
                        unmeasured_segments=database.get('unmeasured_segments', ()))
                else:
                    view = writer(root / f'{key}-{fpga}.json', header, rows)
                indexed[key][fpga] = view
                stack.callback(view.close)
        result = bind(runtime, indexed_routes, indexed_schedule, indexed, platform, metadata=metadata,
                      routes_artifact_sha256=routes_artifact_sha256)
    node = os.environ.get('PYTEST_CURRENT_TEST', '').rsplit(' ', 1)[0]
    position = _COUNTS.get(node, 0)
    _COUNTS[node] = position + 1
    baselines = _BASELINES.get(node)
    if baselines is not None:
        assert position < len(baselines), "unregistered binding baseline invocation"
        expected = baselines[position]
        events = expected['events']
        values = [dict(events['templates'][index], path=path)
                  for path, indices in events['paths'] for index in indices]
        assert json.loads(json.dumps([asdict(row) for row in result])) == values
        if metadata is not None and 'metadata' in expected:
            assert dict(metadata['paths']) == expected['metadata']
    return result


def native_measurements(metadata, records):
    """Import supplied engine scalars through the production native importer."""
    from emuflow.global_event_store import EventBuilder, MetadataBuilder
    from emuflow.global_measurements import import_measurements
    builder = EventBuilder(bound=True)
    paths = MetadataBuilder(builder)
    seen = set()
    try:
        for row in records:
            path = row['path']
            if path not in seen:
                seen.add(path)
                info = metadata['paths'][path]
                paths.append(path, representative_path=info.get('representative_path', path),
                    path_scope=info['path_scope'], clock_domain=info.get('clock_domain'),
                    target_period_ns=info.get('target_period_ns', 10.0),
                    physical_logic_segments_cone_bound=info['physical_logic_segments_cone_bound'],
                    interconnect_delay_model=info.get('interconnect_delay_model'))
            builder.append(EventCheck(path, row['role'], row['event'], 0.0, (), row['required_ns']))
        rows = builder.finish()
        metadata['paths'] = paths.finish()
        output = builder._owner.root / 'engine.tsv'
        output.write_text('endpoint\tarrival_ns\trequired_ns\tslack_ns\n' + ''.join(
            f"o{i}\t{row['arrival_ns']:.17g}\t{row['required_ns']:.17g}\t{row['slack_ns']:.17g}\n"
            for i, row in enumerate(records)))
        return import_measurements(output, rows, verify_arcs=False)
    except BaseException:
        builder._owner.close()
        raise


def build_timing_from_records(runtime, metadata, records):
    measured = native_measurements(metadata, list(records))
    return _native_build_timing(runtime, metadata, measured)
