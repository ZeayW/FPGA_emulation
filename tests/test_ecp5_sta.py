from pathlib import Path
import shutil
import pytest
from emuflow.ecp5_sta import export_ecp5_data_checks, run_ecp5_data_checks, run_ecp5_pair_checks
from emuflow.errors import ValidationError


def graph():
    a=('a','Q'); b=('b','Q'); c=('lut','F'); d=('sink','DI')
    def delay(v): return ((v,v,v),(v,v,v))
    return {'roots':{a:{'kind':'ff','clock_to_q':delay(.2)}, b:{'kind':'ff','clock_to_q':delay(.4)}},
            'captures':{d:{'kind':'ff'}},'dynamic_nodes':{a,b,c,d},'order':[a,b,c,d],
            'edges':[(a,c,delay(2)),(b,c,delay(4)),(c,d,delay(.5))]}


def test_export_retains_shared_fanin_and_separate_delay_arcs(tmp_path):
    g=graph(); export_ecp5_data_checks(g,tmp_path,launches_ns={r:0 for r in g['roots']},deadlines_ns={('sink','DI'):10})
    text=(tmp_path/'physical.v').read_text()
    assert 'M2' in text and text.count('wire e')==3
    assert '4.9' not in (tmp_path/'physical.lib').read_text()
    assert '-max -9 ' in (tmp_path/'physical.sdc').read_text()


def test_missing_constraints_fail_before_export(tmp_path):
    with pytest.raises(ValidationError,match='complete'):
        export_ecp5_data_checks(graph(),tmp_path,launches_ns={},deadlines_ns={})
    assert not (tmp_path/'physical.v').exists()


@pytest.mark.skipif(not shutil.which('sta'),reason='native OpenSTA unavailable')
def test_native_opensta_selects_longer_branch(tmp_path):
    g=graph(); measurements=run_ecp5_data_checks(g,tmp_path,launches_ns={r:0 for r in g['roots']},deadlines_ns={('sink','DI'):10})
    row=measurements['sink','DI']
    assert row['arrival_ns']==pytest.approx(4.9,abs=.001)
    assert row['slack_ns']==pytest.approx(5.1,abs=.001)


@pytest.mark.skipif(not shutil.which('sta'),reason='native OpenSTA unavailable')
def test_native_pairs_preserve_noncritical_launch(tmp_path):
    g=graph();end=('sink','DI');pairs=[(r,end) for r in g['roots']]
    result=run_ecp5_pair_checks(g,tmp_path,pairs=pairs,launches_ns={r:0 for r in g['roots']},deadlines_ns={end:10})
    assert result[(('a','Q'),end)]['arrival_ns']==pytest.approx(2.7,abs=.001)
    assert result[(('b','Q'),end)]['arrival_ns']==pytest.approx(4.9,abs=.001)


def test_pair_binding_rejected_before_export(tmp_path):
    with pytest.raises(ValidationError,match='bound dynamic'):
        run_ecp5_pair_checks(graph(),tmp_path,pairs=[(('wrong','Q'),('sink','DI'))],launches_ns={},deadlines_ns={})
    assert not list(tmp_path.iterdir())
