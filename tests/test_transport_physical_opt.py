import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.ir import EmuIR
from emuflow.io import file_sha256
from emuflow.transport_fanout import buffer_transport_high_fanout_nets
from emuflow.transport_physical_opt import _assign, _cluster, rewire_transport_buffers
from emuflow.xilinx_physical_handoff import write_xilinx_physical_handoff
from tests.test_transport_fanout import _transport_ir
from tests.test_xilinx_physical_handoff import _contracts


def _fixture(root, *, missing=False, equivalent=True, interleaved=True):
    ir = _transport_ir(8)
    buffer_transport_high_fanout_nets(ir, max_fanout=4)
    if not equivalent:
        ir.value['instances'][-1]['parameters']['LUT'] = '01'
    source, handoff = root/'transport.emuir.sqlite', root/'handoff.sqlite'
    ir.save(source)
    packed, placement = _contracts()
    packed['clusters'], packed['cascade_chains'], packed['unplaced_constants'] = [], [], []
    placement['clusters'] = []
    for i, cell in enumerate(ir.value['instances']):
        if missing and cell['id'] == 'sink0000':
            continue
        if cell['id'].startswith('sink'):
            n = int(cell['id'][4:])
            x = (n % 2 if interleaved else n // 4) * 100
        else:
            x = 100 if cell is ir.value['instances'][-1] else 0
        name = '__emuflow_transport__/' + cell['id']
        p, l = _contracts(name)
        p, l = copy.deepcopy(p['clusters'][0]), copy.deepcopy(l['clusters'][0])
        p['id'] = l['cluster'] = f'site{i}'
        l['site'] = l['assignments'][0]['site'] = f'SLICE_X{x}Y{i}'
        l['x'], l['y'] = x, 0
        packed['clusters'].append(p)
        placement['clusters'].append(l)
    write_xilinx_physical_handoff(handoff, packed, placement)
    return source, handoff


class PhysicalTransportTest(unittest.TestCase):
    def test_spatial_rewire_preserves_function_and_capacity(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source, handoff = _fixture(root)
            original = EmuIR.load(source).value
            source_hash, handoff_hash = file_sha256(source), file_sha256(handoff)
            output = root/'optimized.emuir.sqlite'
            report = rewire_transport_buffers(source, handoff, output)
            changed = EmuIR.load(output).value
            self.assertEqual(report['summary']['moved_sinks'], 4)
            self.assertEqual(report['summary']['before_manhattan'], 400)
            self.assertEqual(report['summary']['after_manhattan'], 0)
            self.assertEqual(changed['instances'], original['instances'])
            self.assertEqual(changed['nets'][0], original['nets'][0])
            original_pins = sorted((s['instance'], s['port'], s['bit'])
                                   for n in original['nets'][1:] for s in n['sinks'])
            changed_pins = sorted((s['instance'], s['port'], s['bit'])
                                  for n in changed['nets'][1:] for s in n['sinks'])
            self.assertEqual(original_pins, changed_pins)
            self.assertEqual([len(n['sinks']) for n in changed['nets']], [2, 4, 4])
            self.assertEqual(file_sha256(source), source_hash)
            self.assertEqual(file_sha256(handoff), handoff_hash)
            # Once physically coherent, the same transform is a no-op.
            repeat = rewire_transport_buffers(output, handoff, root/'repeat.emuir.sqlite')
            self.assertEqual(repeat['summary']['moved_sinks'], 0)

    def test_missing_coordinates_skip_whole_group(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source, handoff = _fixture(root, missing=True)
            output = root/'optimized.emuir.sqlite'
            report = rewire_transport_buffers(source, handoff, output)
            self.assertEqual(report['groups'][0]['status'], 'skipped-missing-coordinate')
            self.assertEqual(EmuIR.load(source).value, EmuIR.load(output).value)

    def test_rejects_nonidentity_buffer_without_publishing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source, handoff = _fixture(root, equivalent=False)
            output = root/'optimized.emuir.sqlite'
            with self.assertRaisesRegex(ValidationError, 'identity LUT1'):
                rewire_transport_buffers(source, handoff, output)
            self.assertFalse(output.exists())

    def test_nonimproving_assignment_is_retained(self):
        chosen, before, after = _assign([[0, 100], [100, 0]], [0, 1])
        self.assertEqual((chosen, before, after), ([0, 1], 0, 0))

    def test_cluster_escapes_coincident_buffer_anchors(self):
        points = [(0, 0), (100, 0), (0, 0), (100, 0), (0, 0), (100, 0)]
        chosen, centers, before, after = _cluster(points, [0, 0, 0, 1, 1, 1], [(50, 0)]*2)
        self.assertEqual((before, after), (200, 0))
        self.assertEqual(sorted(centers), [(0, 0), (100, 0)])
        self.assertEqual(chosen.count(0), 3)
        self.assertEqual(chosen.count(1), 3)

    def test_distinct_inputs_cannot_exchange_sinks(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source, handoff = _fixture(root)
            db = sqlite3.connect(source)
            buffers = list(db.execute("SELECT seq FROM cell_records WHERE id LIKE '$emuflow_transport_fanout$%' ORDER BY id"))
            # Move one buffer's input to its own output; it is no longer equivalent.
            out = db.execute('SELECT net_seq FROM endpoint_records WHERE instance_seq=? AND role=0', buffers[1]).fetchone()[0]
            db.execute('UPDATE endpoint_records SET net_seq=?,ordinal=99 WHERE instance_seq=? AND role=1', (out, buffers[1][0]))
            db.commit()
            db.close()
            report = rewire_transport_buffers(source, handoff, root/'optimized.emuir.sqlite')
            self.assertEqual(report['summary']['moved_sinks'], 0)


if __name__ == '__main__':
    unittest.main()
