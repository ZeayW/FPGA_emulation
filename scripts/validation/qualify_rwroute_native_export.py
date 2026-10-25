#!/usr/bin/env python3
"""One-shot complete RWRoute export differential over published immutable stores.

This does not run placement, Java routing, physical validation or global STA.
Only compact input/output identities, counts and measurements survive cleanup.
"""
from __future__ import annotations
import argparse
from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import sys
import tempfile

from emuflow.errors import ValidationError
from emuflow.io import read_json, write_json
from emuflow.native_tools import native_file_generation, resolve_native_executable
from emuflow.sqlite_artifacts import immutable_sqlite_uri
from emuflow.stage_metrics import measure_stage
from emuflow.tool_runtime import NativeToolProcess
from emuflow.xilinx_netlist import open_xilinx_mapped_index
from emuflow.xilinx_physical_handoff import open_xilinx_physical_handoff, validate_xilinx_physical_handoff_pair
from emuflow.xilinx_rwroute import export_rwroute_input

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.rwroute_reference import _export_rwroute_python_reference

COUNTS = ('cells', 'logical_cells', 'virtual_io_cells', 'expanded_lut6_2_cells',
          'transformed_dsp48e2_cells', 'qualified_clock_gates', 'physical_cells',
          'routable_nets', 'excluded_nets')


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4*1024*1024), b''):
            result.update(block)
    return result.hexdigest()


def require_sources(request):
    for name in ('mapped', 'physical'):
        if list(native_file_generation(Path(request[name]['path']))) != request[name]['generation']:
            raise ValidationError('RWRoute differential source generation changed')


def worker(path):
    request = read_json(path)
    if request.get('schema') != 'emuflow.rwroute-export-worker/v1':
        raise ValidationError('unsupported RWRoute differential request')
    arm = request['implementation']
    if arm not in ('reference', 'native'):
        raise ValidationError('unsupported RWRoute differential arm')
    root = Path(request['output']); scratch = root/'tmp'; scratch.mkdir()
    for key in ('TMPDIR', 'TMP', 'TEMP', 'SQLITE_TMPDIR', 'XDG_CACHE_HOME'):
        os.environ[key] = str(scratch)
    tempfile.tempdir = None
    require_sources(request)
    mapped_path, physical_path = (Path(request[name]['path']) for name in ('mapped', 'physical'))
    with ExitStack() as stack:
        if arm == 'reference':
            packed, placement = open_xilinx_physical_handoff(
                physical_path, validated_sha256=request['physical']['sha256'])
            validate_xilinx_physical_handoff_pair(packed, placement)
            if packed.get('status') != 'pass' or placement.get('status') != 'pass' or packed.get('top') != request['top']:
                raise ValidationError('RWRoute differential physical boundary is not published/passing')
            mapped = stack.enter_context(open_xilinx_mapped_index(mapped_path))
            if mapped.top != request['top']:
                raise ValidationError('RWRoute differential mapped top disagrees')
            arguments = {'digests': request['digests'], 'virtual_io_contract': request['io'],
                         'resource_limits': request['limits']}
            function = lambda: _export_rwroute_python_reference(
                mapped, packed, placement, physical_path, physical_path, scratch/'input.tsv', **arguments)
        else:
            # Only bounded header projections precede the native arm; no Python
            # mapped/physical views, cascades or constant populations are opened.
            with closing(sqlite3.connect(immutable_sqlite_uri(mapped_path), uri=True)) as db:
                row = db.execute("SELECT json_extract(value,'$') FROM metadata WHERE key='top_json'").fetchone()
                if row is None or row[0] != request['top']:
                    raise ValidationError('RWRoute differential mapped top disagrees')
            with closing(sqlite3.connect(immutable_sqlite_uri(physical_path), uri=True)) as db:
                row = db.execute(
                    "SELECT json_extract(p.value_json,'$.top'),json_extract(p.value_json,'$.status'),"
                    "json_extract(l.value_json,'$.status') FROM document p JOIN document l "
                    "ON l.name='placement' WHERE p.name='packed'"
                ).fetchone()
                if row != (request['top'], 'pass', 'pass'):
                    raise ValidationError('RWRoute differential physical boundary is not published/passing')
            function = lambda: export_rwroute_input(
                mapped_path, physical_path, scratch/'input.tsv',
                source_sha256=request['digests'], virtual_io_contract=request['io'],
                resource_limits=request['limits'])
        result = measure_stage(root, 'rwroute-export', function)
        if result.get('status') != 'pass' or result['logical_cells'] < request['minimum_logical_cells']:
            raise ValidationError('RWRoute differential design scale/status disagrees')
        if arm == 'native' and result.get('implementation_id') != 'native-rwroute-export-v2':
            raise ValidationError('RWRoute differential did not execute the native owner')
        require_sources(request)
        # Exact whole output identity, outside the measured exporter interval.
        output = scratch/'input.tsv'
        write_json(root/'result.json', {
            'schema': 'emuflow.rwroute-export-arm/v1', 'status': 'pass', 'implementation': arm,
            'counts': {name: result[name] for name in COUNTS},
            'tsv_sha256': digest(output), 'tsv_bytes': output.stat().st_size,
            'export_metrics': read_json(root/'stage-metrics/rwroute-export.json'),
            'runtime_breakdown': result['runtime_breakdown'],
            'native_metrics': result.get('native_metrics'),
        })


def qualify(args):
    output = args.out.resolve()
    if output.exists():
        raise ValidationError('RWRoute differential output must be new')
    binary = Path(resolve_native_executable('emuflow-native'))
    install = Path(os.environ.get('EMUFLOW_NATIVE_ROOT', str(binary.parent.parent)))
    source = {'declared_commit': args.source_commit, 'native_binary_sha256': digest(binary)}
    if args.source_commit:
        if (install/'SOURCE_COMMIT').read_text().strip() != args.source_commit:
            raise ValidationError('RWRoute differential source/runtime commit disagrees')
        import emuflow
        if Path((install/'PYTHON_SOURCE_PATH').read_text().strip()).resolve() != Path(emuflow.__file__).resolve().parent.parent:
            raise ValidationError('RWRoute differential Python source/runtime disagrees')
        built = install/'NATIVE_BUILD_SOURCE_COMMIT'
        source['native_build_commit'] = built.read_text().strip() if built.exists() else args.source_commit
    inputs = {}
    for name, path in (('mapped', args.mapped.resolve()), ('physical', args.physical.resolve())):
        generation = list(native_file_generation(path))
        inputs[name] = {'path': str(path), 'generation': generation, 'sha256': digest(path)}
        if list(native_file_generation(path)) != generation:
            raise ValidationError('RWRoute differential input changed while hashing')
    io = read_json(args.virtual_io_contract) if args.virtual_io_contract else None
    limits = read_json(args.resource_limits) if args.resource_limits else None
    output.mkdir(parents=True)
    scratch = output/'tmp'; scratch.mkdir()
    common = dict(inputs, schema='emuflow.rwroute-export-worker/v1', top=args.expected_top,
                  io=io, limits=limits, minimum_logical_cells=args.minimum_logical_cells,
                  digests={'mapped_sha256': inputs['mapped']['sha256'],
                           'packed_sha256': inputs['physical']['sha256'],
                           'placement_sha256': inputs['physical']['sha256']})
    result = {'schema': 'emuflow.rwroute-export-differential/v1', 'status': 'failed',
              'scope': 'complete-TSV-export-only; no placement/routing/physical/global-timing/full-flow qualification',
              'input': inputs, 'source': source, 'top': args.expected_top,
              'minimum_logical_cells': args.minimum_logical_cells,
              'configuration': {'virtual_io': io, 'resource_limits': limits},
              'arm_order': ['reference', 'native'], 'source_hash_precedes_arms': True,
              'timing_excludes_output_hashing': True, 'arms': {}}
    try:
        for arm in ('reference', 'native'):
            root = output/arm; root.mkdir()
            request = scratch/(arm+'.json')
            write_json(request, dict(common, implementation=arm, output=str(root)))
            command = [os.path.abspath(sys.executable), str(Path(__file__).resolve()), '--worker-request', str(request)]
            with NativeToolProcess(str(binary), command, environment=dict(os.environ), scratch_parent=scratch,
                                   timeout_ms=21600000, termination_grace_ms=10000) as process:
                completed = process.finish()
            if completed.returncode:
                raise ValidationError(arm+' RWRoute export failed: '+completed.stdout)
            result['arms'][arm] = read_json(root/'result.json')
            result['arms'][arm]['worker_metrics'] = completed.native_metrics
            # The exact worker and all supervised descendants have exited.
            shutil.rmtree(root/'tmp')
        left, right = (result['arms'][arm] for arm in ('reference', 'native'))
        if any(left[field] != right[field] for field in ('counts', 'tsv_bytes', 'tsv_sha256')):
            raise ValidationError('RWRoute complete output differs from the independent reference')
        require_sources(common)
        result['status'] = 'pass'
        return result
    except BaseException as error:
        result['error'] = str(error)[-32768:]
        raise
    finally:
        # NativeToolProcess completes cancellation/group reaping before here.
        try:
            for arm in ('reference', 'native'):
                path = output/arm/'tmp'
                if path.exists(): shutil.rmtree(path)
            shutil.rmtree(scratch)
        except OSError as error:
            result['status'] = 'failed'; result['cleanup_error'] = str(error)
            raise
        finally:
            write_json(output/'qualification.json', result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker-request', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--mapped', type=Path); parser.add_argument('--physical', type=Path)
    parser.add_argument('--out', type=Path); parser.add_argument('--expected-top')
    parser.add_argument('--virtual-io-contract', type=Path); parser.add_argument('--resource-limits', type=Path)
    parser.add_argument('--minimum-logical-cells', type=int, default=0)
    parser.add_argument('--source-commit')
    args = parser.parse_args()
    def cancel(signum, frame): raise KeyboardInterrupt('RWRoute differential cancelled by '+str(signum))
    for sig in (signal.SIGINT, signal.SIGTERM): signal.signal(sig, cancel)
    if args.worker_request: worker(args.worker_request); return
    if not args.mapped or not args.physical or not args.out or not args.expected_top or args.minimum_logical_cells < 0:
        parser.error('--mapped, --physical, --out and --expected-top are required; minimum logical cells must be nonnegative')
    result = qualify(args)
    print(json.dumps({'status': result['status'], 'counts': result['arms']['native']['counts']}))


if __name__ == '__main__': main()
