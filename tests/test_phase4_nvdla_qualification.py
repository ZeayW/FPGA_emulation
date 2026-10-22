"""Representative path completeness is independent of all-cut structural coverage."""
import copy
import unittest
from unittest.mock import patch
from tests.test_phase4_nvdla_timing import fixture, evaluate
from tests.test_phase4_nvdla_compare import compare, prefix, qualification


def preflight(values):
    originals, assignment, projected, _routes, _schedule, _board, nets, _platform = values
    return evaluate.validate_original_projection(iter(originals), assignment, projected, original_nets=nets)


class QualificationTest(unittest.TestCase):
    def test_local_fanout_and_unenumerated_cut_preserve_complete_member_accounting(self):
        values = fixture(remote_output=True, include_local=True)
        values[1]['cut_nets'].append({'net': 'other_branch', 'source_fpgas': ['a'], 'sink_fpgas': ['b']})
        result = preflight(values)
        self.assertEqual((result['original_paths'], result['local_paths'], result['cross_paths']), (4,1,3))
        self.assertEqual((result['projected_members'], result['covered_cut_nets'], result['uncovered_cut_nets']), (3,1,1))
        self.assertEqual(set(result['per_clock']), {'core','csb'})
        self.assertNotIn('total_negative_slack_ns', result)

    def test_fail_closed_member_and_constraint_mutations(self):
        for mutation in ('missing','extra','duplicate','clock','required','ownership','fixed','population'):
            with self.subTest(mutation=mutation):
                values = fixture(remote_output=True, include_local=True)
                originals, assignment, projected = values[:3]
                record = projected['paths'][0]
                if mutation == 'missing': record['compressed_path_ids'].remove('lo')
                if mutation == 'extra': record['compressed_path_ids'].append('ghost')
                if mutation == 'duplicate': record['compressed_path_ids'].append('lo')
                if mutation == 'clock': record['clock_domain'] = 'csb'
                if mutation == 'required': record['slack_ns'] += 1
                if mutation == 'ownership': assignment['instance_assignment']['capture'] = 'a'
                if mutation == 'fixed': record['member_fixed_delay_ns'] = [3.,8.]
                if mutation == 'population': originals.pop()
                with self.assertRaises(ValueError): preflight(values)

    def test_nonzero_uncovered_requires_independent_dual_qualification(self):
        report, evidence = prefix(), qualification()
        for value in (report['timing']['cut_path_projection'], evidence['original_accounting']):
            value.update(uncovered_cut_nets=100, covered_cut_nets=800)
        compare.check_prefix(report, evidence)
        with self.assertRaises(ValueError): compare.check_prefix(report)
        for part in ('original_accounting','structural_qualification'):
            bad = copy.deepcopy(evidence)
            bad[part]['status'] = 'failed'
            with self.assertRaises(ValueError): compare.check_prefix(report, bad)
        evidence['original_accounting']['uncovered_cut_nets'] = 0
        with self.assertRaises(ValueError): compare.check_prefix(report, evidence)

    def test_historical_gate_exception_cannot_hide_computation_changes(self):
        current = {'scripts/phase4_nvdla_compare.py':'new', 'src/emuflow/sta.py':'same'}
        provenance = {'schema':'emuflow.nvdla-prefix-provenance/v1',
                      'source_sha256':{**current, 'scripts/phase4_nvdla_compare.py':'old'},
                      'runtime':{}, 'files':{'rtl':'sealed'}, 'benchmark':'benchmark'}
        with patch.object(compare,'code_identity',return_value=current), \
             patch.object(compare,'runtime_identity',return_value={}), \
             patch.object(compare,'digest',return_value='sealed'), \
             patch.object(compare,'read',return_value={}), patch.object(compare,'check_benchmark'):
            compare.verify_provenance(provenance, replay_source=current)
            with self.assertRaises(ValueError): compare.verify_provenance(provenance)
            provenance['source_sha256']['src/emuflow/sta.py'] = 'other'
            with self.assertRaises(ValueError): compare.verify_provenance(provenance, replay_source=current)

    def test_platform_normalization_preserves_hardware_contract(self):
        from emuflow.platform import Platform
        raw = compare.read(compare.ROOT / 'platforms/virtual/rapidwright_xcvu19p_4fpga_mesh.json')
        normalized = Platform.from_dict(raw).to_dict()
        compare.check_platform(raw)
        compare.check_platform(normalized)
        for key, value in [('utilization_limit', .5), ('capacity', {**normalized['fpgas'][0]['capacity'], 'lut': 1})]:
            altered = copy.deepcopy(normalized)
            altered['fpgas'][0][key] = value
            with self.assertRaises(ValueError): compare.check_platform(altered)
        altered = copy.deepcopy(normalized)
        altered['links'][0]['tdm_delay_model']['ratio_coefficient_ns'] += .1
        with self.assertRaises(ValueError): compare.check_platform(altered)
        altered = copy.deepcopy(normalized)
        altered['unexpected_field'] = True
        with self.assertRaises(ValueError): compare.check_platform(altered)
