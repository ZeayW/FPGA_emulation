import unittest

from emuflow.errors import ValidationError
from emuflow.snapshot_timing_windows import iter_snapshot_timing_windows


class SnapshotTimingWindowTests(unittest.TestCase):
    def model(self):
        population = dict(board='board0', launches={
            'q': dict(kind='state', identity='q'),
            'rx': dict(kind='cut', identity='net32'),
            'host': dict(kind='host', identity='in')}, captures={
            'tx': dict(kind='cut'), 'd': dict(kind='state'), 'out': dict(kind='host')})
        interface = dict(board='board0', imported_nets=['net' + str(i) for i in range(33)])
        def transfer(source, target, epoch, word, capture, update):
            return dict(source=source, target=target, epoch=epoch, word=word,
                        capture_ns=capture, update_ns=update)
        protocol = dict(scope='simulated_protocol_events_not_measured_link',
            reset_release_ns={'board0': 0, 'board1': 0}, cycles=[
                dict(cycle=0, host_latch_ns=10, commits_ns={'board0': 100, 'board1': 90}, transfers=[
                    transfer('board0', 'board1', 0, 0, 20, 40),
                    transfer('board1', 'board0', 0, 0, 21, 45),
                    transfer('board1', 'board0', 0, 1, 21, 50),
                    transfer('board0', 'board1', 1, 0, 70, 80),
                    transfer('board1', 'board0', 1, 0, 71, 85),
                    transfer('board1', 'board0', 1, 1, 71, 86)]),
                dict(cycle=1, host_latch_ns=110, commits_ns={'board0': 200, 'board1': 190}, transfers=[
                    transfer('board0', 'board1', 2, 0, 120, 150),
                    transfer('board1', 'board0', 2, 1, 121, 160)])])
        return population, interface, protocol

    def windows(self, p=None, i=None, trace=None, initial=None):
        default_p, default_i, default_trace = self.model()
        return list(iter_snapshot_timing_windows(p or default_p, i or default_i,
            trace or default_trace, initial_ready_ns={'q': 1, 'rx': 2} if initial is None else initial))

    def test_real_event_edges_replace_uniform_local_period(self):
        rows = self.windows()
        self.assertEqual([row['capture_edge_ns'] for row in rows], [20, 70, 100, 120, 200])
        self.assertEqual(rows[0]['launches_ns'], dict(q=1, rx=2, host=10))
        self.assertEqual(rows[1]['launches_ns']['rx'], 50)  # word 1, not word 0
        self.assertEqual(rows[2]['captures'], ('d', 'out'))
        self.assertEqual(rows[2]['launches_ns'], dict(q=1, rx=86, host=10))
        self.assertEqual(rows[3]['launches_ns'], dict(q=100, rx=86, host=110))

    def test_same_edge_update_cannot_supply_new_value(self):
        p, i, trace = self.model()
        trace['cycles'][0]['transfers'][-1]['update_ns'] = 100
        self.assertEqual(self.windows(p, i, trace)[2]['launches_ns']['rx'], 50)

    def test_missing_initial_or_word_binding_fails(self):
        for initial in ({}, {'q': 1}, {'q': -1, 'rx': 2}, {'q': float('nan'), 'rx': 2}, {'q': 999, 'rx': 2}):
            with self.subTest(initial=initial), self.assertRaises(ValidationError):
                self.windows(initial=initial)
        p, i, trace = self.model()
        i['imported_nets'].pop()
        with self.assertRaisesRegex(ValidationError, 'missing imported'):
            self.windows(p, i, trace)

    def test_words_cannot_disagree_about_capture(self):
        p, i, trace = self.model()
        trace['cycles'][0]['transfers'].append(dict(source='board0', target='board1', epoch=0,
            word=1, capture_ns=22, update_ns=41))
        with self.assertRaisesRegex(ValidationError, 'disagree'):
            self.windows(p, i, trace)
