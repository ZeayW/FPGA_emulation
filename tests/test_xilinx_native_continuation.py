import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import emuflow.xilinx_native_continuation as continuation
from emuflow.errors import ValidationError


class NativeContinuationTest(unittest.TestCase):
    def fixture(self, root):
        old = root / 'completed'
        original = old / 'rapidwright'
        original.mkdir(parents=True)
        def put(path, value):
            path.write_text(json.dumps(value))
        put(old / 'placement-ir-report.json', {
            'status': 'pass', 'original_instances': 2, 'transport_instances': 1,
            'instances': 3, 'boundary_identity': {'output': str(old / 'boundary.json')},
        })
        put(original / 'mapped-netlist-report.json', {
            'status': 'pass', 'top': 'top', 'output_sha256': 'a' * 64,
        })
        put(original / 'openparf-atomic-source.json', {
            'status': 'pass', 'source': {'mapped_sha256': 'a' * 64},
            'summary': {'atoms': 3},
        })
        put(original / 'placement-region.json', {'global': {'allowed_slrs': ['SLR1']}})
        for name in ['placement.emuir.sqlite', 'logic-segment-identity.json', 'local-path-identity.json']:
            (old / name).touch()
        (original / 'partition.mapped.sqlite').touch()
        spec = {'fpga': 'F', 'completed_partition': str(old),
                'output_dir': str(root / 'output'), 'java_heap_gib': 96}
        for field in continuation._PATH_FIELDS:
            if field not in spec:
                path = root / field
                path.touch()
                spec[field] = str(path)
        put(Path(spec['split_manifest']), {'clock_periods': {'core': 10.0, 'control': 15.0}})
        put(Path(spec['schedule']), {'status': 'pass'})
        return spec

    @contextlib.contextmanager
    def services(self, *, wrong_part=False, tail_error=None):
        mapped = object()
        runtime = {'preserved_runtime': True}
        def bridge(*args, **kwargs):
            kwargs['in_process_artifacts'].update({
                'packed': {'summary': {'cells': 3}}, 'placement': {'summary': {'sites': 2}},
                'packing_validation': {'status': 'pass'}, 'placement_validation': {'status': 'pass'},
            })
            return {'status': 'pass'}
        with contextlib.ExitStack() as stack:
            patch = lambda name, **kwargs: stack.enter_context(mock.patch.object(continuation, name, **kwargs))
            platform = SimpleNamespace(fpgas=[SimpleNamespace(id='F', part='other' if wrong_part else 'part')])
            stack.enter_context(mock.patch.object(continuation.Platform, 'load', return_value=platform))
            stack.enter_context(mock.patch.object(continuation.ArchitectureDB, 'load', return_value=SimpleNamespace(part='part')))
            build = patch('build_virtual_runtime', return_value=runtime)
            opened = patch('open_xilinx_mapped_index', side_effect=lambda *_: contextlib.nullcontext(mapped))
            native = patch('run_xilinx_openparf_atomic_qualification', return_value={'status': 'pass'})
            compact = patch('_compact_openparf_qualification', return_value={'status': 'pass'})
            bridged = patch('materialize_xilinx_openparf_atomic_contract', side_effect=bridge)
            tail = patch('_run_rapidwright_routed_backend_tail',
                         return_value={'status': 'pass', 'result': {'timing': {'critical_path_ns': 7.0}}},
                         side_effect=tail_error)
            validate = patch('validate_physical_partition_result')
            # Explicitly prohibit regenerating a mapping or running synthesis.
            stack.enter_context(mock.patch('emuflow.xilinx_physical_backend.emit_xilinx_mapped_index',
                                           side_effect=AssertionError('must not remap completed circuit')))
            yield SimpleNamespace(mapped=mapped, runtime=runtime, build=build, opened=opened,
                                  native=native, compact=compact, bridge=bridged, tail=tail, validate=validate)

    def test_uses_normal_native_bridge_route_timing_and_validation_once(self):
        with tempfile.TemporaryDirectory() as directory, self.services() as calls:
            spec = self.fixture(Path(directory))
            result = continuation.run_native_partition_continuation(spec)
            record = json.loads((Path(spec['output_dir']) / 'partition-record.json').read_text())
            self.assertEqual(result['status'], 'pass')
            self.assertIn('not whole-design Phase7C', result['scope'])
            self.assertEqual(calls.build.call_args.args[2], {'core': 10.0, 'control': 15.0})
            self.assertIs(calls.native.call_args.kwargs['mapped_value'], calls.mapped)
            self.assertEqual(calls.native.call_args.kwargs['provider_manifest_path'], Path(spec['provider_manifest']))
            self.assertEqual(calls.bridge.call_args.kwargs['provider_manifest_path'], Path(spec['provider_manifest']))
            self.assertEqual(calls.native.call_args.kwargs['allowed_slrs'], ['SLR1'])
            self.assertIs(calls.tail.call_args.kwargs['mapped_value'], calls.mapped)
            self.assertIs(calls.tail.call_args.kwargs['runtime'], calls.runtime)
            self.assertEqual(calls.tail.call_args.kwargs['java_heap_gib'], 96)
            self.assertEqual(calls.tail.call_args.kwargs['local_identity_path'],
                             Path(spec['completed_partition']) / 'local-path-identity.json')
            for method in [calls.opened, calls.native, calls.compact, calls.bridge, calls.tail, calls.validate]:
                self.assertEqual(method.call_count, 1)
            self.assertIs(calls.validate.call_args.args[0], calls.tail.return_value['result'])
            self.assertEqual(record['original_cells'], 2)
            self.assertEqual(record['transport_cells'], 1)

    def test_provider_is_required_before_any_native_work_or_output(self):
        with tempfile.TemporaryDirectory() as directory, self.services() as calls:
            spec = self.fixture(Path(directory))
            del spec['provider_manifest']
            with self.assertRaisesRegex(ValidationError, 'provider_manifest'):
                continuation.run_native_partition_continuation(spec)
            calls.native.assert_not_called()
            self.assertFalse(Path(spec['output_dir']).exists())

    def test_existing_output_is_never_overwritten_or_restarted(self):
        with tempfile.TemporaryDirectory() as directory, self.services() as calls:
            spec = self.fixture(Path(directory))
            output = Path(spec['output_dir'])
            output.mkdir()
            (output / 'result.json').write_text('original evidence')
            with self.assertRaises(FileExistsError):
                continuation.run_native_partition_continuation(spec)
            calls.native.assert_not_called()
            self.assertEqual((output / 'result.json').read_text(), 'original evidence')

    def test_device_mismatch_fails_before_native_placement(self):
        with tempfile.TemporaryDirectory() as directory, self.services(wrong_part=True) as calls:
            spec = self.fixture(Path(directory))
            with self.assertRaisesRegex(ValidationError, 'device part'):
                continuation.run_native_partition_continuation(spec)
            calls.native.assert_not_called()
            result = json.loads((Path(spec['output_dir']) / 'result.json').read_text())
            self.assertEqual(result['status'], 'failed')

    def test_failed_route_publishes_no_successful_partition_record(self):
        with tempfile.TemporaryDirectory() as directory, self.services(tail_error=RuntimeError('unrouted')) as calls:
            spec = self.fixture(Path(directory))
            with self.assertRaisesRegex(RuntimeError, 'unrouted'):
                continuation.run_native_partition_continuation(spec)
            output = Path(spec['output_dir'])
            self.assertFalse((output / 'partition-record.json').exists())
            self.assertEqual(json.loads((output / 'result.json').read_text())['status'], 'failed')
            calls.validate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
