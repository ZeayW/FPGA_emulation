"""Consume native clock/RX witnesses without recreating original FF endpoints."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
HEADER = ('phase_word\tquery_hex\tsource_kind\tsource_hex\tlaunch_edge\tmeasurement\t'
          'requirement_ns\tdata_ns\tsampling_cost_ns\tclock_adjustment_ns\tstart_hex\t'
          'end_hex\tstart_clock_hex\tend_clock_hex\tlaunch_ns\tcapture_ns')


@pytest.fixture(scope='module')
def reader(tmp_path_factory):
    output = tmp_path_factory.mktemp('native-cost-reader')/'native-word-costs'
    compiler = shutil.which('c++') or shutil.which('clang++')
    assert compiler, 'C++17 compiler required'
    subprocess.run([compiler, '-std=c++17', '-O2', '-I', str(ROOT/'src/native'),
                    str(ROOT/'tests/native_word_costs_test.cpp'),
                    str(ROOT/'src/native/emuflow_core/native_word_costs.cpp'),
                    '-o', str(output)], check=True)
    return output


def fields(*, kind='dut-clock', source='core_generated', edge='fall', phase='3',
           measurement='native-clock-aware-reference'):
    hx = lambda text: text.encode().hex()
    result = [phase, hx('native_serializer/D[3]'), kind, hx(source), edge, measurement]
    if measurement == 'native-clock-aware-reference':
        result += ['1.0', '6.0', '2.5', '-3.5', hx('renamed_dsp/CLK'),
                   hx('native_serializer/D[3]'), hx('core_generated'), hx('phy_div'), '-50.0', '5.0']
    else:
        result += ['']*10
    return result


def consume(reader, rows, *, header=HEADER, final_newline=True, summary=False):
    text = header+'\n'+'\n'.join('\t'.join(row)for row in rows)+('\n'if final_newline and rows else '')
    return subprocess.run([str(reader), 'summary'if summary else'rows'],
                          input=text, text=True, capture_output=True)


def test_signed_clock_cost_and_native_witness_survive_input(reader):
    done = consume(reader, [fields()])
    assert done.returncode == 0 and not done.stderr
    row,counts = map(json.loads,done.stdout.splitlines())
    assert row['source']=='core_generated' and row['launch_edge']==2
    assert row['evidence']==dict(requirement_ns=1.,data_ns=6.,sampling_cost_ns=2.5,
                                clock_adjustment_ns=-3.5,start_pin='renamed_dsp/CLK',
                                end_pin='native_serializer/D[3]',start_clock='core_generated',
                                end_clock='phy_div',launch_ns=-50.,capture_ns=5.)
    assert counts==dict(rows=1,measured=1,no_timed_path=0,context_required=0)


def test_missing_path_and_unknown_input_context_have_no_fake_zero_cost(reader):
    done = consume(reader, [fields(measurement='native-no-timed-path'),
        fields(kind='ports',source='service_input',edge='',phase='-1',
               measurement='native-source-context-required')],final_newline=False)
    assert done.returncode == 0 and not done.stderr
    rows = list(map(json.loads,done.stdout.splitlines()))
    assert rows[0]['measurement']==1 and rows[0]['evidence'] is None
    assert rows[1]['measurement']==2 and rows[1]['evidence'] is None
    assert rows[-1]==dict(rows=2,measured=0,no_timed_path=1,context_required=1)


def test_native_rx_infrastructure_and_declared_inputs_keep_distinct_roles(reader):
    kinds = [('rx','s9272'),('infrastructure','payload_active'),('declared-input','dla_reset_n')]
    done = consume(reader,[fields(kind=kind,source=source,edge='')for kind,source in kinds])
    assert done.returncode==0 and not done.stderr
    rows = list(map(json.loads,done.stdout.splitlines()))[:-1]
    assert [(row['source_kind'],row['source'])for row in rows]==[(1,'s9272'),(2,'payload_active'),(5,'dla_reset_n')]


def test_utf8_pin_and_query_identities_are_preserved(reader):
    row = fields()
    row[1] = '接口[3]/雪\\'.encode().hex()
    row[10] = '优化后的寄存器/C'.encode().hex()
    done = consume(reader,[row])
    assert done.returncode==0
    observed = json.loads(done.stdout.splitlines()[0])
    assert observed['query']=='接口[3]/雪\\'
    assert observed['evidence']['start_pin']=='优化后的寄存器/C'


@pytest.mark.parametrize('column,value,message',[
    (0,'-2','phase'),(0,'03','phase'),(0,'9223372036854775808','phase'),
    (1,'ff','UTF-8'),(1,'00','NUL'),(1,'abc','encoding'),
    (2,'original-ff','kind'),(4,'','launch edge'),(4,'both','launch edge'),
    (5,'optimized-away','classification'),(6,'100','1 ns'),
    (7,'-1','adjustment'),(7,'nan','finite'),(8,'inf','finite'),
    (8,'2.5 garbage','number'),(9,'0','adjustment'),(10,'','encoding'),
    (12,'','encoding'),(15,'1e309','number'),
])
def test_invalid_provider_rows_do_not_become_physical_evidence(reader,column,value,message):
    row = fields();row[column] = value
    done = consume(reader,[row])
    assert done.returncode!=0 and message in done.stderr


@pytest.mark.parametrize('row,message',[
    (fields(kind='rx',source='register-name',edge=''),'RX source role'),
    (fields(kind='rx',source='s7',edge='rise'),'launch-edge'),
    (fields(kind='ports',source='untimed',edge=''),'context'),
    (fields(kind='dut-clock',measurement='native-source-context-required'),'context'),
    (fields()[:-1],'column count'),(fields()+['extra'],'column count'),
])
def test_source_roles_and_record_width_cannot_be_reinterpreted(reader,row,message):
    done = consume(reader,[row])
    assert done.returncode!=0 and message in done.stderr


@pytest.mark.parametrize('measurement',['native-no-timed-path','native-source-context-required'])
def test_absent_measurements_reject_numerical_and_witness_columns(reader,measurement):
    kwargs = dict(kind='ports',source='service',edge='')if measurement.endswith('required')else{}
    for column,value in [(7,'0'),(10,'ff')]:
        row = fields(measurement=measurement,**kwargs);row[column] = value
        done = consume(reader,[row])
        assert done.returncode!=0 and 'unmeasured row' in done.stderr


def test_header_and_record_bound_are_enforced_before_graph_accumulation(reader):
    done = consume(reader,[fields()],header=HEADER.replace('sampling_cost_ns','delay_ns'))
    assert done.returncode!=0 and 'header' in done.stderr
    row = fields();row[1] = 'a'*300000
    done = consume(reader,[row])
    assert done.returncode!=0 and 'bound' in done.stderr
    done = consume(reader,[fields()]*20000,summary=True)
    assert done.returncode==0 and len(done.stdout)<200
    assert json.loads(done.stdout)['measured']==20000


def test_actual_tcl_export_shape_is_consumed_by_the_native_reader(reader,tmp_path):
    from tests.test_native_word_timing import execute,TCL86
    if TCL86 is None:
        pytest.skip('Tcl 8.6 required')
    done,path = execute(tmp_path,r'''
set sources [emuflow_native_word_sources $role_path]
set stream [open $output_path w]
emuflow_native_word_cost_header $stream
emuflow_native_word_tx_costs $sources $stream 3 schedule-entry {native_serializer/D[3]}
close $stream
''')
    assert not done.stderr
    parsed = subprocess.run([str(reader),'summary'],input=path.read_text(),text=True,capture_output=True)
    assert parsed.returncode==0 and not parsed.stderr
    assert json.loads(parsed.stdout)==dict(rows=9,measured=4,no_timed_path=2,context_required=3)
