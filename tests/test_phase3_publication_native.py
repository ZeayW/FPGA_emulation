"""Whole final-directory identity, failed-publication and source-binding gates."""
import copy
from contextlib import closing
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.common.errors import ValidationError
from emuflow.common.io import read_json
from emuflow.partition.phase3_storage import read_phase3_assignment_view
from tests.phase3_store_fixture import write_cluster_fixture, cluster_column_facts
from tests.native_partition_facts import materialize_facts
from tests.native_partition_kernel_driver import publish_partition as publish
from emuflow.partition.phase3_assignment_index import validate_phase3_assignment_index_against_emuir
from tests.native_partition_facts import close_published_semantics
from tests.test_phase3_semantics_native import selected, build


def report(assignment):
    return {'schema':'emuflow.phase3-report/v1','status':'pass','phase':3,
        **{key:assignment[key] for key in ('design','platform','provider','seed')},
        'artifacts':{'clusters':'clusters.json','assignment':'assignment.json','assignment_index':'assignment.sqlite',
            'constraints':'constraints.normalized.json','report':'phase3_report.json'},
        'validation':{'cut_nets':assignment['metrics']['cut_nets']}}


def test_complete_directory_uses_native_transaction_and_reloads_without_private_sources(selected,tmp_path):
    actual=build(selected);output=tmp_path/'published'
    with patch('tests.native_assignment_publication_fixture.publish_native_assignment_descriptor', side_effect=AssertionError('host descriptor publisher')):
        final=publish(output,selected[0],selected[2],actual,report(actual))
    assert final['finalization_implementation_id']=='native-partition-publication-v1'
    close_published_semantics(actual)
    loaded=read_phase3_assignment_view(output/'assignment.json',include_instance_assignment=True)
    with closing(loaded['instance_assignment']) as owners:
        assert dict(owners)==dict(selected[5]['instance_assignment'])
    assert list(loaded['cut_nets'])==list(selected[5]['cut_nets'])
    assert loaded['semantic_contract_sha256']==selected[5]['semantic_contract_sha256']
    clusters=read_json(output/'clusters.json')
    assert 'clusters' not in clusters
    expected_catalog=tmp_path/'expected-cluster-facts/clusters.json'
    write_cluster_fixture(expected_catalog,materialize_facts(selected[2]))
    assert cluster_column_facts(output/'clusters.json')==cluster_column_facts(expected_catalog)
    proof=validate_phase3_assignment_index_against_emuir(output/'assignment.sqlite',selected[0].path,
        expected_design=actual['design'],expected_platform=actual['platform'])
    assert proof['status']=='pass'
    assert not list(tmp_path.glob('.partition-publish-*'))
    assert not list(tmp_path.glob('.phase3-publication-*'))


@pytest.mark.parametrize('kind',['report','partition-count','source-generation','existing-directory'])
def test_invalid_transaction_does_not_publish_partial_directory(selected,tmp_path,kind):
    actual=build(selected);summary=report(actual);output=tmp_path/'rejected'
    if kind=='report':summary['design']='another-design'
    elif kind=='partition-count':actual['partitions'][0]['instance_count']+=1
    elif kind=='source-generation':
        path=actual['cut_nets']._cuts._sink_endpoints.native_source.path
        path.chmod(path.stat().st_mode^0o100)
    else:
        output.mkdir();(output/'preserved').write_bytes(b'previous-complete-generation')
    with pytest.raises(ValidationError):publish(output,selected[0],selected[2],actual,summary)
    if kind=='existing-directory':assert list(output.iterdir())==[output/'preserved'] and (output/'preserved').read_bytes()==b'previous-complete-generation'
    else:assert not output.exists()
    assert not list(tmp_path.glob('.partition-publish-*'))
    assert not list(tmp_path.glob('.phase3-publication-*'))
