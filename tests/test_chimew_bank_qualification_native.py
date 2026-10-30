"""Independent typed bank-assignment semantic validation in the shared core."""
from unittest.mock import patch

import pytest

from emuflow.common.errors import ValidationError
from emuflow_tools.transport.chimew.chimew_bank_channel import evaluate_chimew_bank_channel_assignment
from emuflow_tools.transport.chimew import chimew_bank_channel_storage as inputs
from emuflow_tools.transport.chimew import chimew_bank_channel_report_storage as reports
from emuflow_tools.transport.chimew.chimew_qualification import validate_chimew_bank_channel_report_artifact
from tests.chimew_bank_report_fixture import write_chimew_bank_channel_report_storage
from tests.chimew_qualification_reference import validate_chimew_bank_channel_report_artifact as reference
from tests.test_chimew_bank_native_store import document


def test_bank_qualification_does_not_expand_python_groups_or_assignments(tmp_path):
    raw = document()
    with inputs.write_chimew_bank_channel_storage(tmp_path / 'input.json', raw) as source:
        with evaluate_chimew_bank_channel_assignment(source, output_path=tmp_path / 'report.json') as report:
            with (
                patch.object(source, 'validated_problem', side_effect=AssertionError('Python model validation')),
                patch.object(inputs._RawGroupSequence, '__iter__', side_effect=AssertionError('Python group walk')),
                patch.object(reports._AssignmentSequence, '__iter__', side_effect=AssertionError('Python assignment walk')),
            ):
                result = validate_chimew_bank_channel_report_artifact(source, report)
            assert result['status'] == 'pass'
            assert result['input_sha256'] == source.logical_sha256
            assert result['report_sha256'] == report.logical_sha256
            assert result['metrics']['groups'] == len(raw['groups'])


@pytest.mark.parametrize('mutation', ['none', 'order', 'tiny-roundoff', 'namespace', 'coverage', 'cost', 'priority', 'metrics'])
def test_native_bank_qualification_matches_independent_reference(tmp_path, mutation):
    raw = document()
    with inputs.write_chimew_bank_channel_storage(tmp_path / 'input.json', raw) as source:
        with evaluate_chimew_bank_channel_assignment(source, output_path=tmp_path / 'report.json') as report:
            value = report.materialize()
        if mutation == 'order':
            value['assignments'].reverse()
        elif mutation == 'tiny-roundoff':
            value['metrics']['stage1_physical_site_cost'] += 1e-10
        elif mutation == 'namespace':
            value['design'] = 'wrong-source'
        elif mutation == 'coverage':
            value['assignments'][0]['group'] = 'unknown-group'
        elif mutation == 'cost':
            value['assignments'][0]['bank_cost'] += 1.0
        elif mutation == 'priority':
            value['direction_priority']['near'] = 'b_to_a'
        elif mutation == 'metrics':
            value['metrics']['certified_matchings'] += 1
        valid = mutation in ('none', 'order', 'tiny-roundoff')
        if valid:
            expected = reference(raw, value)
        else:
            with pytest.raises(ValidationError):
                reference(raw, value)
        with write_chimew_bank_channel_report_storage(tmp_path / 'candidate.json', value) as candidate:
            if valid:
                actual = validate_chimew_bank_channel_report_artifact(source, candidate)
                assert actual['provenance'] == expected['provenance']
                assert actual['metrics']['signals'] == expected['metrics']['signals']
            else:
                with pytest.raises(ValidationError):
                    validate_chimew_bank_channel_report_artifact(source, candidate)
