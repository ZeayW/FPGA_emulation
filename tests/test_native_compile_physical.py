"""Production compiler dispatches the complete physical owner directly.

External tool doubles cover integration; terminal qualification uses NVDLA.
"""
import json
from pathlib import Path
import pytest
from emuflow.common.errors import EmuFlowError
from tests.native_build import emuflow_native, tlr_router
from tests.test_physical_flow_native import setup, write_flow_transport_tool


def test_baseline_compile_owns_physical_without_a_python_phase_adapter(tmp_path,monkeypatch):
    from emuflow.flow.multi_fpga_flow import run_multi_fpga_flow
    from tests.yosys_reference import import_yosys_json
    from tests.partition_fixture import write_fixture_solution
    from tests.xilinx_timing_fixture import write_mock_xilinx_timing_contract
    root=Path(__file__).resolve().parents[1]
    board=json.loads((root/'platforms/virtual/static_exact_acceptance_2fpga.json').read_text())
    for fpga in board['fpgas']:
        fpga['part']='xcvu19p-test'
        fpga['capacity']={r:100 for r in ('lut','ff','bram18k','dsp48','carry8','uram288')}
        fpga['utilization_limit']=.25
    platform=tmp_path/'board.json';platform.write_text(json.dumps(board))
    source=root/'examples/yosys/static_exact_chain.json'
    solution=tmp_path/'initial.part'
    write_fixture_solution(import_yosys_json(source,top='static_exact_chain',clocks=['clk']),platform,solution,
        cut_mode='static-exact-combinational',balance_tolerance=1.,max_cross_fpga_dependency_depth=2)
    vendor_sources=setup(tmp_path/'external-tools')
    vendor=next(row['path'] for row in vendor_sources['sources'] if row['role']=='backend.tool')
    output=tmp_path/'flow'
    yosys=write_flow_transport_tool(tmp_path/'yosys',output/'split')
    extractor=tmp_path/'extractor'
    extractor.write_text((root/'tests/fixtures/fake_opensta_paths.py').read_text().replace('f"path-{index}"','f"q_reg/Q->q_reg/D#{index:08d}"').replace('    records.append(',
        "    emuir_hex=','.join(name.encode().hex() for name in ['q','n0','n1','n2','n3','n4','n5','d'])\n    records.append("))
    extractor.chmod(0o755)
    # The test intentionally stops at the real Phase 7C boundary: this controlled
    # input extractor supplies no terminal global timing engine evidence.
    options=dict(tritonpart_solution=solution,platform_path=platform,output_dir=output,yosys_json=source,
        top='static_exact_chain',clocks=['clk'],partition_provider='tritonpart',balance_tolerance=1.,
        timing_driven=False,clock_periods={'clk':10.},opensta=str(extractor),
        router=str(tlr_router()),frame_slots=32,equivalence_cycles=2,cut_mode='static-exact-combinational',
        max_cross_fpga_dependency_depth=2,mapping_profile='xilinx-ultrascaleplus-open-v1',
        architecture_timing_db=write_mock_xilinx_timing_contract(tmp_path/'mock-timing.json'),
        physical=True,physical_backend='vivado',physical_vivado=vendor,yosys=str(yosys),
        global_sta_executable=str(extractor))
    with pytest.raises(EmuFlowError) as failure:run_multi_fpga_flow(**options)
    assert (output/"native-compile-result.json").is_file(),str(failure.value)
    control=json.loads((output/'native-compile-result.json').read_text())
    physical=next((row for row in control['stages'] if row['stage']=='physical'),None)
    assert physical is not None,control['operation']
    assert physical['status']=='pass',control['operation']
    assert physical['implementation_id']=='native-multi-fpga-physical-v1'
    assert control['stages'][-1]['stage']=='signoff' and control['stages'][-1]['status']=='fail'
    assert not control['native_data_plane_complete']
    result=json.loads((output/'physical/multi-fpga-physical-flow-report.json').read_text())
    assert result['implementation_id']=='native-multi-fpga-physical-v1'
    assert (output/'stage-metrics/phase7.json').is_file()

    assert not (output/'.native-physical-request').exists()

    summary=json.loads((output/'physical/physical-summary.json').read_text())
    for group in ('boundary_identities','boundary_timing','logic_segment_timing','local_path_timing'):
        assert set(summary[group])==set(result['expected_fpgas'])
        assert all((output/'physical'/ref['path']).is_file() for ref in summary[group].values())
