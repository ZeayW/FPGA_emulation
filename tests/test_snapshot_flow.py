import json
import os
from unittest.mock import patch

import pytest

from emuflow.errors import ValidationError
from emuflow.snapshot_flow import main,parser,_prepare


def arguments(tmp_path):
    source=tmp_path/'dut.v';source.write_text('module dut(input clk); endmodule\n')
    vectors=tmp_path/'vectors.json';vectors.write_text('[{}]')
    tools=tmp_path/'tools';tools.mkdir()
    for name in ('yosys','yosys-abc','nextpnr-ecp5','ecppack','iverilog','vvp','openroad','sta'):
        p=tools/name;p.write_text('#!/bin/sh\nexit 0\n');p.chmod(0o755)
    return ['--source',str(source),'--top','dut','--clock','clk','--target-period-ns','40',
        '--vectors',str(vectors),'--undefined-state-seed','1','--timing-cycle','0',
        '--setup-uncertainty-ns','0','--tools',str(tools),'--openroad',str(tools/'openroad'),
        '--sta',str(tools/'sta'),'--toolchain-id','explicit-fixture-install','--out',str(tmp_path/'run')]


def test_invalid_contract_rejected_before_creating_output(tmp_path):
    argv=arguments(tmp_path)
    args=parser().parse_args(argv)
    args.target_period_ns=float('nan')
    with pytest.raises(ValidationError):_prepare(args)
    assert not (tmp_path/'run').exists()
    args.target_period_ns=40;args.timing_cycle=1
    with pytest.raises(ValidationError):_prepare(args)


def test_one_shot_keeps_only_summary_restores_environment_and_never_promotes(tmp_path):
    argv=arguments(tmp_path);original=os.environ.get('TMPDIR')
    def execute(args,sources,vectors,tools,root,report):
        assert os.environ['TMPDIR'].startswith(str(root))
        (root/'large-intermediate').write_text('temporary')
        report['status']='checks_finished_qualification_pending'
    def cleanup(root):
        import shutil
        shutil.rmtree(root)
    with patch('emuflow.snapshot_flow._execute',side_effect=execute), \
         patch('emuflow.snapshot_flow._remove_joined_scratch',side_effect=cleanup):
        assert main(argv)==0
    assert os.environ.get('TMPDIR')==original
    assert [p.name for p in (tmp_path/'run').iterdir()]==['result.json']
    result=json.loads((tmp_path/'run/result.json').read_text())
    assert not result['full_flow_qualified'] and not result['hardware_qualified']
    assert (tmp_path/'dut.v').exists()
    with pytest.raises(ValidationError,match='already exists'):main(argv)


def test_failure_keeps_error_and_cleanup_refusal_is_not_success(tmp_path):
    argv=arguments(tmp_path)
    with patch('emuflow.snapshot_flow._execute',side_effect=ValidationError('missing physical pair')), \
         patch('emuflow.snapshot_flow._remove_joined_scratch',side_effect=ValidationError('live consumer')):
        assert main(argv)==1
    result=json.loads((tmp_path/'run/result.json').read_text())
    assert result['status']=='failed' and result['error']=='missing physical pair'
    assert result['cleanup_error']=='live consumer' and not result['scratch_removed']


def test_root_cli_exposes_the_same_explicit_contract(tmp_path):
    from emuflow.cli import _build_parser,_dispatch
    args=_build_parser().parse_args(['ulx3s-qualify']+arguments(tmp_path))
    assert args.clock=='clk' and args.timing_cycle==0
    with patch('emuflow.snapshot_flow.run',return_value=1) as execute:
        assert _dispatch(args)==1
    assert execute.call_args.args==(args,)


def test_vectors_allow_declared_unused_ports_but_never_guess_live_inputs():
    from types import SimpleNamespace
    from emuflow.snapshot_flow import _connected_vectors
    ir=SimpleNamespace(value=dict(ports=[dict(id=n,direction='input',width=1) for n in ('clk','data','unused')],
        nets=[dict(cut_class='primary_input',drivers=[dict(instance=None,port='data')])]))
    assert _connected_vectors(ir,'clk',[dict(data=1,unused=0)])==[dict(data=1)]
    with pytest.raises(ValidationError,match='missing connected inputs'):
        _connected_vectors(ir,'clk',[dict(unused=0)])
    with pytest.raises(ValidationError,match='unknown inputs'):
        _connected_vectors(ir,'clk',[dict(data=1,typo=0)])
    with pytest.raises(ValidationError,match='out-of-range'):
        _connected_vectors(ir,'clk',[dict(data=1,unused=2)])
