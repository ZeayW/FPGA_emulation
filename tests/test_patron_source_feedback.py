"""Version 15 must reject hidden regressions and feed their paths back."""
import json
import os
import shutil
from unittest.mock import patch

import pytest

from emuflow.io import write_json
from emuflow.opensta import run_opensta_path_database
from emuflow.partition import (build_clusters,build_partition_assignment,
    normalize_partition_constraints,CUT_MODE_STATIC_EXACT)
from emuflow.partition_ir import load_partition_ir
from emuflow.partition_pressure import build_partition_pressure_model
from emuflow.patron_source_checkpoints import check_patron_source_assignments
from emuflow.patron_source_feedback import merge_source_witnesses,run_patron_source_feedback,source_checkpoint_rank
from emuflow.platform import Platform
from emuflow.routing import normalize_route_constraints
from emuflow.errors import EmuFlowError,ValidationError
from tests.test_patron_source_checkpoints import context
from tests.test_phase5 import _platform_value,_link


pytestmark=pytest.mark.skipif(not os.environ.get('EMUFLOW_OPENSTA_LAUNCH_ENGINE'),reason='qualified real OpenSTA required')


@pytest.fixture
def design(tmp_path):
    ctx=context(tmp_path);source=ctx.source_bindings[0].path;timing=tmp_path/'timing.json'
    run_opensta_path_database(source,timing,clocks={'clk':10.},timing_io=dict(
        input_groups=[dict(clock='clk',delay_ns=0.,ports=['a','b'])],
        output_groups=[dict(clock='clk',delay_ns=0.,ports=['q'])]),
        executable=os.environ['EMUFLOW_OPENSTA_LAUNCH_ENGINE'],_stream_output=True,clock_budget_policy='engine-edge-v1')
    platform=Platform.from_dict(_platform_value('board',['a','b'],[_link('ab','a','b')]))
    ir=load_partition_ir(source)
    constraints=normalize_partition_constraints(dict(schema='emuflow.partition-constraints/v1',
        min_used_fpgas=2,balance_tolerance=1.),ir,platform)
    clusters=build_clusters(ir,constraints,cut_mode=CUT_MODE_STATIC_EXACT,_native_compact_clusters=True)
    routes=normalize_route_constraints(None,platform)
    model=build_partition_pressure_model(ir,platform,clusters,constraints,json.loads(timing.read_text()),routes,
        independent_validation=False,timing_database_path=timing,native_store_model=True)
    def assignment(good=False):
        owners={record['id']:('b'if 'launch_b'in record['instances'] else'a')for record in clusters['clusters']}if good else{
            f'c{i:06d}':('a'if i%2==0 else'b')for i in range(len(clusters['clusters']))}
        return build_partition_assignment(ir,platform,clusters,constraints,owners,'fixture',7,
            _include_semantic_contract=False,_stream_semantic_records=True,_defer_derived_records=True)
    return ctx,model,clusters,assignment(False),assignment(True),timing


def test_native_merge_deduplicates_nominal_witnesses_and_preserves_clock_checks(design,tmp_path):
    ctx,model,_,bad,_,_=design
    first=tmp_path/'first';report=check_patron_source_assignments(model,ctx,[('bad',bad)],first)
    shutil.copytree(first,tmp_path/'copy')
    merged=merge_source_witnesses(ctx,[first,tmp_path/'copy'],tmp_path/'merged')
    assert merged['input_rows']==2*report['feedback']['paths']
    assert merged['duplicate_rows']>0
    assert merged['paths']<merged['input_rows']
    from emuflow.sta import iter_sta_path_database_paths
    selector=tmp_path/'merged/paths.json'
    rows=list(iter_sta_path_database_paths(selector,json.loads(selector.read_text())))
    assert len(rows)==merged['paths']
    assert all(row['fixed_delay_ns']<1 and row['clock_budget']['check_margin_ns']>=0 for row in rows)


def test_bad_later_witness_owner_cannot_publish_a_partial_bank(design,tmp_path):
    ctx,model,_,bad,_,_=design
    first=tmp_path/'first';check_patron_source_assignments(model,ctx,[('bad',bad)],first)
    shutil.copytree(first,tmp_path/'broken')
    raw=tmp_path/'broken/evaluation/feedback.tsv';raw.write_bytes(raw.read_bytes()[:-1])
    with pytest.raises(EmuFlowError,match='truncated'):
        merge_source_witnesses(ctx,[first,tmp_path/'broken'],tmp_path/'rejected')
    assert not(tmp_path/'rejected').exists()


def test_real_witness_guided_rounds_accept_only_a_complete_graph_improvement(design,tmp_path):
    ctx,model,clusters,bad,_,_=design
    final,report=run_patron_source_feedback(model,ctx,clusters,bad,tmp_path/'feedback',max_moves=1,max_rounds=2)
    assert source_checkpoint_rank(report['final_metrics'])<source_checkpoint_rank(report['initial_metrics'])
    assert report['candidate_selection']['accepted_rounds']>=1
    assert final['provider']=='patron-v15-source-feedback-v1'


def test_rejected_candidate_adds_a_previously_hidden_path_before_the_next_proposal(design,tmp_path):
    ctx,model,clusters,bad,good,_=design
    calls=[]
    def propose(*args,**kwargs):
        proposal=args[5];calls.append(proposal.expected_paths)
        candidate=bad if len(calls)==1 else good
        return candidate,dict(model_counts=dict(paths=proposal.expected_paths),move_count=1,batch_count=0)
    with patch('emuflow.partition_pressure.run_partition_pressure_native',side_effect=propose):
        final,report=run_patron_source_feedback(model,ctx,clusters,good,tmp_path/'feedback',max_rounds=2)
    assert len(calls)==2 and calls[1]>calls[0]
    assert not report['rounds'][0]['accepted']
    assert report['rounds'][0]['rank']>report['candidate_selection']['initial_timing_rank']
    assert report['candidate_selection']['selected']=='initial'
    assert final['cluster_assignment']==good['cluster_assignment']


def test_phase3_v15_publishes_full_source_selection_and_feedback_artifacts(design,tmp_path):
    from emuflow.phase3 import run_phase3
    from emuflow.phase3_storage import pack_phase3_assignment,write_indexed_phase3_clusters
    ctx,model,clusters,bad,_,timing=design
    write_json(tmp_path/'board.json',model.platform.to_dict())
    write_json(tmp_path/'constraints.json',model.constraints)
    write_indexed_phase3_clusters(tmp_path/'clusters.json',clusters)
    write_json(tmp_path/'initial.json',pack_phase3_assignment(bad,clusters))
    report=run_phase3(model.ir.path,tmp_path/'board.json',tmp_path/'phase3',provider='patron',
        constraints_path=tmp_path/'constraints.json',timing_database_path=timing,
        patron_initial_assignment_path=tmp_path/'initial.json',patron_algorithm_version=15,
        patron_source_context=ctx,patron_max_moves=1)
    assert report['patron_algorithm_version']==15
    assert report['algorithm_validation']['qualification']=='complete-source-checkpoint-acceptance'
    assert (tmp_path/'phase3'/report['artifacts']['patron_source_feedback']).is_file()
    decision=report['algorithm_validation']['candidate_selection']
    assert decision['candidate_timing_rank']<=decision['initial_timing_rank']
