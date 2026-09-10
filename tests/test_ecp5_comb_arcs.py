import unittest
from emuflow.ecp5_timing_coverage import qualify_ecp5_comb_arcs
from emuflow.errors import ValidationError


class CombArcTests(unittest.TestCase):
    def model(self, mode='LOGIC'):
        inputs = ['A', 'B', 'C', 'D'] + (['FCI'] if mode == 'CCU2' else ['M', 'F1', 'FXA', 'FXB'])
        outputs = ['F', 'FCO' if mode == 'CCU2' else 'OFX']
        cell = {'type': 'TRELLIS_COMB', 'parameters': {'MODE': mode},
                'connections': {pin: [i] for i, pin in enumerate(inputs + outputs)},
                'port_directions': {pin: 'input' if pin in inputs else 'output' for pin in inputs + outputs}}
        arcs = {(a, z): ((.1, .2, .3), (.2, .3, .4)) for a in inputs for z in outputs
                if mode == 'CCU2' or z == 'OFX' or a in 'ABCD'}
        return {'modules': {'top': {'cells': {'lut': cell}}}}, {
            'delay_connectivity_checked': True, 'cells': {'lut': {'iopaths': arcs}}}

    def test_complete_logic_and_carry(self):
        for mode, count in [('LOGIC', 12), ('CCU2', 10)]:
            r, d = self.model(mode); result = qualify_ecp5_comb_arcs(r, d)
            self.assertEqual(result['checked_arcs'], count)
            self.assertFalse(result['global_timing_qualified'])

    def test_each_missing_or_reversed_arc_fails(self):
        for mode in ('LOGIC', 'CCU2'):
            r, d = self.model(mode)
            for key in list(d['cells']['lut']['iopaths']):
                for reverse in (False, True):
                    rr, dd = self.model(mode); arcs = dd['cells']['lut']['iopaths']
                    value = arcs.pop(key)
                    if reverse: arcs[tuple(reversed(key))] = value
                    with self.subTest(mode=mode, key=key, reverse=reverse), self.assertRaises(ValidationError):
                        qualify_ecp5_comb_arcs(rr, dd)

    def test_disconnected_pins_do_not_require_arcs(self):
        r, d = self.model(); r['modules']['top']['cells']['lut']['connections']['A'] = []
        arcs = d['cells']['lut']['iopaths']
        del arcs['A', 'F']; del arcs['A', 'OFX']
        self.assertEqual(qualify_ecp5_comb_arcs(r, d)['checked_arcs'], 10)

    def test_unknown_stateful_mode_and_unchecked_input_fail(self):
        for mutation in ('DPRAM', 'unknown', 'write', 'unchecked', 'carry', 'direction'):
            r, d = self.model(); cell = r['modules']['top']['cells']['lut']
            if mutation in ('DPRAM', 'unknown'): cell['parameters']['MODE'] = mutation
            elif mutation == 'write': cell['connections']['WCK'] = [99]
            elif mutation == 'unchecked': d['delay_connectivity_checked'] = False
            elif mutation == 'carry': cell['connections']['FCI'] = [99]
            else: cell['port_directions']['A'] = 'output'
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                qualify_ecp5_comb_arcs(r, d)
