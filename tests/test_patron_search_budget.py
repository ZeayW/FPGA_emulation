"""Explicit deterministic optimizer budgets preserve complete result validation."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_patron_ratio_cache import HARNESS, ROOT
from tests.native_build import patron_refiner
from emuflow.partition_pressure import _parse_patron_native_output

WRAPPER = HARNESS.split('int main(int argc,char** argv) {')[0] + r'''
int main(int argc,char** argv) {
  auto model=fixture(std::stoi(argv[1]),true);
  model.max_moves=model.clusters*4;model.max_sweeps=4;
#ifdef BUDGET
  sweep_prefix_budget.limit=std::stoull(argv[3]);
#endif
  run_scalable(model,argv[2],true);
}
'''


def build(tmp_path,source,name,bounded):
    wrapper=tmp_path/(name+'.cpp');binary=tmp_path/name
    wrapper.write_text(WRAPPER.replace('PATRON_SOURCE',str(source)))
    subprocess.run([shutil.which('c++'),'-std=c++17','-O2',
        *(['-DBUDGET'] if bounded else []),'-I'+str(ROOT/'src/native'),str(wrapper),'-o',str(binary)],check=True)
    return binary


def test_budgeted_sweep_is_an_exact_prefix_and_default_output_unchanged(tmp_path):
    old=tmp_path/'reference-source.cpp'
    old.write_bytes(subprocess.check_output(['git','show','397a:src/native/patron_refiner.cpp'],cwd=ROOT))
    previous=build(tmp_path,old,'previous',False)
    current=build(tmp_path,ROOT/'src/native/patron_refiner.cpp','current',True)
    for n in (8,24):
        baseline=tmp_path/'baseline.out';normal=tmp_path/'normal.out'
        subprocess.run([str(previous),str(n),str(baseline)],capture_output=True,check=True)
        subprocess.run([str(current),str(n),str(normal),'0'],capture_output=True,check=True)
        assert normal.read_bytes()==baseline.read_bytes()
        original_steps=[line for line in baseline.read_text().splitlines() if line.startswith('STEP ')]
        for limit in (1,3,10):
            output=tmp_path/'budget.out'
            subprocess.run([str(current),str(n),str(output),str(limit)],capture_output=True,check=True)
            lines=output.read_text().splitlines()
            assert f'SEARCH_BUDGET sweep-prefix-clusters/v1 {limit} {limit} budget-exhausted' in lines
            steps=[line for line in lines if line.startswith('STEP ')]
            assert steps==original_steps[:len(steps)]
            assert not any(line.startswith('BATCH ') for line in lines)
            assert lines[-1]=='END'
            parsed=_parse_patron_native_output(output,{'clusters':[str(i) for i in range(4*n)],
                                                       'parts':[str(i) for i in range(4)]})
            assert parsed[-1]['search_budget']['cluster_limit']==limit
            assert parsed[-1]['search_budget']['convergence_claim'] is False
            assert parsed[-1]['search_budget']['later_refinement_stages']=='not-executed'
            assert len([line for line in lines if line.startswith('ASSIGN ')])==4*n
            assignment=[i%4 for i in range(4*n)]
            for step in steps:
                fields=step.split();assert fields[2]=='0';assignment[int(fields[4])]=int(fields[6])
            assert [int(line.split()[2]) for line in lines if line.startswith('ASSIGN ')]==assignment


@pytest.mark.parametrize('value',['0','-1','x','18446744073709551616'])
def test_invalid_budget_does_not_start_native(value,tmp_path):
    result=subprocess.run([str(patron_refiner()),'--sweep-prefix-clusters',value,
        str(tmp_path/'missing.in'),str(tmp_path/'out')],capture_output=True)
    assert result.returncode==2
    assert not (tmp_path/'out').exists()


def test_completed_result_requires_explicit_budget_command_identity():
    from emuflow.patron_result import command_search_budget
    from emuflow.errors import ValidationError
    assert command_search_budget(['tool','--summary-output','in','out']) is None
    assert command_search_budget(['tool','--sweep-prefix-clusters','100000','--progress','in','out'])==100000
    for options in (['--sweep-prefix-clusters','0'],['--sweep-prefix-clusters'],
                    ['--sweep-prefix-clusters','1','--sweep-prefix-clusters','2'],['--unknown']):
        with pytest.raises(ValidationError):
            command_search_budget(['tool',*options,'in','out'])


def test_budget_output_contract_survives_native_handoff_and_rejects_relabeling(tmp_path):
    from unittest.mock import patch
    from tests import test_partition_pressure as fixtures
    from emuflow.io import write_json
    from emuflow.errors import ValidationError
    from emuflow.partition_pressure import _write_patron_native_input, run_partition_pressure_native
    from emuflow.patron_result import seal_completed_result
    f=fixtures.PartitionPressureTest();f.setUp()
    from emuflow.partition import build_clusters, build_partition_assignment
    from emuflow.partition_pressure import build_partition_pressure_model
    f.clusters=build_clusters(f.ir,f.constraints,cut_mode='static-exact-combinational')
    f.initial=build_partition_assignment(f.ir,f.platform,f.clusters,f.constraints,
        {c['id']:('a' if c['instances'][0] in {'u0','u2'} else 'b') for c in f.clusters['clusters']},
        provider='fixture',seed=7)
    f.model=build_partition_pressure_model(f.ir,f.platform,f.clusters,f.constraints,f.timing,f.route_constraints)
    native_input,native_output=tmp_path/'in',tmp_path/'out'
    indexes=_write_patron_native_input(native_input,f.platform,f.clusters,f.constraints,
        f.route_constraints,f.model,f.initial,len(f.model['clusters']),14)
    binary=patron_refiner().resolve()
    command=[str(binary),'--summary-output','--sweep-prefix-clusters','1',str(native_input),str(native_output)]
    subprocess.run(command,capture_output=True,check=True)
    receipt=tmp_path/'receipt.json';write_json(receipt,{'status':'completed','exit_code':0,'command':command})
    value=seal_completed_result(native_input=native_input,native_output=native_output,binary=binary,
        native_source=ROOT/'src/native/patron_refiner.cpp',receipt=receipt,producer_commit='a'*40,
        clusters=indexes['clusters'],parts=indexes['parts'],context={'input':native_input})
    manifest=tmp_path/'manifest.json';write_json(manifest,value)
    # Publication/source-commit validation is independently tested. This unit
    # exercises the post-publication consumer against actual native output.
    with patch('emuflow.patron_result.validate_completed_result',return_value=value):
        assignment,trace=run_partition_pressure_native(f.ir,f.platform,f.clusters,f.constraints,
            f.route_constraints,f.model,f.initial,completed_result_path=manifest,
            algorithm_version=14,retain_trace_seals=False)
        assert trace['search_budget']['cluster_limit']==1
        assert trace['search_budget']['convergence_claim'] is False
        value['execution']['command'][3]='2'
        with pytest.raises(ValidationError,match='budget differs'):
            run_partition_pressure_native(f.ir,f.platform,f.clusters,f.constraints,
                f.route_constraints,f.model,f.initial,completed_result_path=manifest,
                algorithm_version=14,retain_trace_seals=False)
