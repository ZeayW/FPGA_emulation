"""The normal word provider joins implementation and native observations."""
import csv
import json
from pathlib import Path

import pytest

from emuflow.errors import ValidationError
from emuflow.io import file_sha256
from emuflow.native_word_observation import FILES
from emuflow.vivado_lineage import LINEAGE_POLICY
from emuflow.vivado_physical import run_indexed_vivado_partition
from tests.test_native_word_source_catalog import source


def test_word_provider_uses_native_snapshot_and_retains_real_control_failure(source,monkeypatch):
    root=source.root;output=root/'physical';part='xcvu19p-fsva3824-2-e'
    ir=root/'physical-ir.sqlite';ir.write_text('indexed IR owned by the lowering stage')
    functions=root/'functions.sqlite';functions.write_text('mapped source functions')
    netlist=root/'partition.v';netlist.write_text('module partition; endmodule\n')
    plan=root/'word-plan.json';wiring=root/'wiring.json'
    plan.write_text('{}\n');wiring.write_text('{}\n')
    mapped=source.mapped['fpga0']
    mapped_report=dict(optimization_policy=LINEAGE_POLICY,emitted_instances=5,output_sha256=file_sha256(netlist),
        mapped_index=dict(path=str(mapped),sha256=file_sha256(mapped)),
        source_functions=dict(path=str(functions),sha256=file_sha256(functions)))
    class IR:
        instances=range(5)
        value={'design':{'top':'partition'},'ports':[{'id':'clk'},{'id':'fabric_clk'}]}
        def __init__(self,path):pass
        def __enter__(self):return self
        def __exit__(self,*_):pass
    monkeypatch.setattr('emuflow.vivado_physical.IndexedPhysicalIR',IR)
    monkeypatch.setattr('emuflow.emuir_storage.is_emuir_sqlite',lambda p:True)
    monkeypatch.setattr('emuflow.emuir_compact.is_compact_emuir',lambda p:True)
    monkeypatch.setattr('emuflow.native_tools.require_native_schema',lambda s:None)
    monkeypatch.setattr('emuflow.vivado_backend._resolve_vivado',lambda e:'vivado')
    monkeypatch.setattr('emuflow.vivado_backend.vivado_runtime_xdc',lambda *args:'create_clock -period 100 [get_ports clk]\n')
    def old_query(*args,**kwargs):raise AssertionError('original DUT pin resolution must not run')
    monkeypatch.setattr('emuflow.vivado_backend.write_vivado_boundary_timing_query',old_query)
    monkeypatch.setattr('emuflow.vivado_physical._import_routed_measurement',old_query)
    monkeypatch.setattr('emuflow.vivado_physical._word_infrastructure_timing',old_query)
    def word_top(ir,output,**kwargs):
        for name in ('native-word-banks.sv','physical_top.sv','native-word-fixed-io.xdc','fixed-io-query.tsv','native-word-boundaries.tsv'):
            (output/name).write_text('native word physical preparation\n')
        (output/'native-word-clocks.tsv').write_text('clock_hex\tport_hex\tperiod_ns\n')
        return dict(part=part,top='physical_top',hierarchy_prefix='mapped_partition',active_banks=2,
            fpga='fpga0',data_pairs=40,forwarded_clock_pairs=2)
    monkeypatch.setattr('emuflow.vivado_word_top.write_vivado_word_top',word_top)
    def lineage(mapped,functions,inventories,path,**kwargs):
        assert set(inventories)=={'synthesized','optimized','routed'}
        assert all(p.is_file()for p in inventories.values())
        path.write_text('observed mapped objects, including changed primitive types\n')
        return dict(output=str(path),sha256=file_sha256(path),bytes=path.stat().st_size,stages={'routed':dict(
            placed_objects=6,source_cells_with_lineage=4,unretained_source_cells=1,source_cells_with_multiple_objects=1)})
    monkeypatch.setattr('emuflow.vivado_lineage.import_cell_lineage',lineage)
    calls=[]
    def execute(executable,script,args,out,name):
        calls.append(script.name)
        (out/name).write_text('native tool completed\n')
        if script.name=='implement_indexed_partition.tcl':
            manifest=json.loads((out/'native-session.json').read_text())
            assert manifest['queries']=={}and manifest['word_source_directory']==str(out/'native-word-sources')
            assert 'set emuflow_native_word_observation "true"'in(out/'native-session.tcl').read_text()
            metrics=dict(part=part,vivado_version='2024.2',dut_clock_count=1,mapped_cells=5,
                routed_cells=6,physical_cells=8,infrastructure_cells=2,optimization_cells=0,
                lut=0,ff=8,carry8=0,dsp48=0,uram288=0,ramb18_cells=0,ramb36_cells=0,
                unrouted_nets=0,drc_violations=0,drc_warnings=0,virtual_io_pairs=42,fabric_period_ns=5.0,
                wns_ns=-.3,critical_path_ns=5.6,dut_wns_ns=50.,dut_delay_ns=2.,fabric_wns_ns=-.3,
                fabric_delay_ns=5.6,fabric_to_dut_wns_ns=50.,fabric_to_dut_delay_ns=2.)
            (out/'implementation_metrics.tsv').write_text('metric\tvalue\n'+''.join(f'{k}\t{v}\n'for k,v in metrics.items()))
            (out/'timing_summary.rpt').write_text('WNS(ns) TNS(ns) Failing Total WHS THS HF HT\n-.3 -1.0 1 8 .010 0 0 8\n')
            for n in ('mapped_cells.tsv','optimized_mapped_cells.tsv','routed_mapped_cells.tsv',
                      'route_status.rpt','drc.rpt','utilization.rpt','routed.dcp','word-data-budget-metrics.tsv'):
                (out/n).write_text('retained native implementation\n')
        else:
            assert script.name=='observe_native_word.tcl'and script.parent.name=='recipe'
            assert Path(args[0])==output/'routed.dcp'and Path(args[1])==output/'native-word-sources'
            for n in FILES:(out/n).write_text('native observation rows\n')
            counts=dict(dut_clocks=1,infrastructure_clocks=1,observed_rx_origins=0,requested_rx_origins=1,
                missing_rx_origins=1,repeat_words=6,capture_queries=4,tx_contexts=3)
            (out/'native-observation-metrics.tsv').write_text('metric\tvalue\n'+''.join(f'{k}\t{v}\n'for k,v in counts.items()))
            (out/'missing-rx-roles.tsv').write_text('source_origin\ns9\n')
            (out/'native-control-checks.tsv').write_text('delay_type\tmeasurement\tslack_ns\tdata_ns\tstart_hex\tend_hex\n'+
                'max\tnative-path\t-.3\t5.6\t61\t62\nmin\tnative-path\t.010\t.2\t63\t64\n')
            for n,scope in [('native-dut-hold.tsv','dut-state-to-state'),('native-rx-infrastructure-hold.tsv','rx-to-infrastructure')]:
                (out/n).write_text('scope\tmeasurement\tslack_ns\tdata_ns\tstart_hex\tend_hex\n'+scope+'\tnative-no-timed-path\t\t\t\t\n')
        return dict(status='pass',log=str(out/name))
    monkeypatch.setattr('emuflow.vivado_physical.execute_vivado',execute)
    runtime=dict(fabric_clock={'period_ns':5.},virtual_dut_clocks={'clk':{'target_period_ns':10.,'nominal_period_ns':320.}},
        timing_model={'fabric_clock_port':'fabric_clk','dut_clock_ports':['clk']})
    report=run_indexed_vivado_partition(fpga='fpga0',part=part,ir_path=ir,mapped_verilog_path=netlist,
        mapped_report=mapped_report,runtime=runtime,original_cells=2,transport_cells=3,output_dir=output,
        boundary_identity_path=source.boundaries['fpga0'],word_phy_plan_path=plan,word_wiring_path=wiring,
        word_schedule_path=source.schedule_path,dut_frequency_mhz=10.)
    assert calls==['implement_indexed_partition.tcl','observe_native_word.tcl']
    assert report['provider']=='indexed-phase6-vivado-word-physical-v5'
    assert report['result']['timing']['timing_met']is False
    assert report['word_phy_implementation']['infrastructure_timing']['fixed_clock_contract_met']is False
    assert report['native_word_observation']['counts']['missing_rx_origins']==1
    assert report['result']['cell_accounting']['unretained_source_cells']==1
    assert report['implementation_options']['retain_routed_checkpoint']is True
    assert not{'boundary_timing','local_path_timing','logic_segment_timing'}&set(report)
    assert not(output/'routed-query-preparation.json').exists()
    from emuflow.multi_fpga_physical_flow import _require_native_word_evidence
    record=dict(fpga='fpga0',part=part,stages={'vivado_implementation':report,'word_phy_implementation':report['word_phy_implementation']})
    hashes={key:report['input_identity'][key]['sha256']for key in ('word_phy_plan','word_wiring')}
    assert _require_native_word_evidence(record,hashes)
    (output/'native-word-observation/native-costs.tsv').write_text('changed observations\n')
    with pytest.raises(ValidationError,match='stream changed'):_require_native_word_evidence(record,hashes)
