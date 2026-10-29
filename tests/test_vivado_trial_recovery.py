"""Only discarded, restored optional trials may retain nonfatal diagnostics."""
import pytest

from emuflow.errors import EmuFlowError
from emuflow.vivado_physical import inspect_vivado_log


CW='CRITICAL WARNING: [Constraints 18-11755] Invalid physical equation\n'
ERROR='ERROR: [Common 17-39] route_design failed\n'
BEGIN='EMUFLOW_NATIVE_TRIAL begin locality-route\n'
DISCARD='EMUFLOW_NATIVE_TRIAL discard locality-route\n'
RESTORE='EMUFLOW_NATIVE_TRIAL restore locality-route\n'
ACCEPT='EMUFLOW_NATIVE_TRIAL accept locality-route\n'
SCRIPT='implement_indexed_partition.tcl'


def inspect(tmp_path,text,code=0,script=SCRIPT):
    path=tmp_path/'vivado.log';path.write_text(text)
    return inspect_vivado_log(path,code,script)


def test_restored_trial_retains_diagnostics_and_allows_successful_baseline(tmp_path):
    result=inspect(tmp_path,BEGIN+CW+ERROR+DISCARD+'INFO: reopening retained checkpoint\n'+RESTORE+'INFO: final reports completed\n')
    assert result['critical_warnings']==result['recovered_trial_critical_warnings']==1
    assert result['errors']==result['recovered_trial_errors']==1
    assert result['blocking_critical_warnings']==result['blocking_errors']==0
    assert result['recovered_trials']==1


@pytest.mark.parametrize('text,code,script',[
    (BEGIN+CW+ACCEPT,0,SCRIPT),
    (BEGIN+CW+DISCARD,0,SCRIPT),
    (BEGIN+CW+DISCARD+RESTORE+CW,0,SCRIPT),
    (BEGIN+CW+DISCARD+CW+RESTORE,0,SCRIPT),
    (CW+BEGIN+DISCARD+RESTORE,0,SCRIPT),
    (BEGIN+ERROR+DISCARD+RESTORE+ERROR,0,SCRIPT),
    (BEGIN+CW+DISCARD+RESTORE,1,SCRIPT),
    (BEGIN+CW+DISCARD+RESTORE,0,'other.tcl'),
    (RESTORE,0,SCRIPT),
    (BEGIN+BEGIN+DISCARD+RESTORE,0,SCRIPT),
    (BEGIN+DISCARD+RESTORE+BEGIN+ACCEPT,0,SCRIPT),
    (BEGIN+'EMUFLOW_NATIVE_TRIAL discard postroute-critical-cell\n'+RESTORE,0,SCRIPT),
    ('EMUFLOW_NATIVE_TRIAL begin unknown\n',0,SCRIPT),
    (BEGIN,0,SCRIPT),
    (ERROR,0,SCRIPT),
])
def test_unrecovered_or_final_diagnostics_and_invalid_receipts_remain_fatal(tmp_path,text,code,script):
    with pytest.raises(EmuFlowError):inspect(tmp_path,text,code,script)


def test_query_size_advice_is_not_double_subtracted_in_a_restored_trial(tmp_path):
    result=inspect(tmp_path,BEGIN+'CRITICAL WARNING: [Vivado 12-4439] Large collection\n'+CW+DISCARD+RESTORE)
    assert result['critical_warnings']==2
    assert result['query_scale_advisories']==1
    assert result['recovered_trial_critical_warnings']==1
    assert result['blocking_critical_warnings']==0
