"""The public physical API sends bounded locators to one native owner."""
import copy
import json
from pathlib import Path
from unittest.mock import patch
import pytest
from emuflow.common.errors import ValidationError
from emuflow.flow.multi_fpga_physical_flow import run_multi_fpga_physical_flow, validate_multi_fpga_physical_report
from tests.test_physical_flow_native import setup


def test_public_host_hands_entire_flow_to_native_without_python_graph_stages(tmp_path):
    request=setup(tmp_path)
    paths={r['role']:Path(r['path']) for r in request['sources']}
    with patch('emuflow.ir.ir.EmuIR.load',side_effect=AssertionError('host graph load')), \
         patch('emuflow.physical.lowering.run_placement_ir_lowering_from_split',side_effect=AssertionError('host lowering')), \
         patch('emuflow.physical.transport_synthesis.synthesize_transport',side_effect=AssertionError('host synthesis')), \
         patch('emuflow.physical.vivado.vivado_physical.run_indexed_vivado_partition',side_effect=AssertionError('host backend')):
        result=run_multi_fpga_physical_flow(tmp_path/'split',paths['board'],paths['schedule-selector'],tmp_path/'physical',
            backend='vivado',yosys=str(paths['yosys']),vivado=str(paths['backend.tool']),workers=2)
    assert result['implementation_id']=='native-multi-fpga-physical-v1'
    summary=json.loads((tmp_path/'physical/physical-summary.json').read_text())
    assert validate_multi_fpga_physical_report(result,summary)==result['summary']
    broken=copy.deepcopy(summary);broken['fpgas'][0]['original_cells']+=1
    with pytest.raises(ValidationError,match='coverage disagrees'):validate_multi_fpga_physical_report(result,broken)
    broken=copy.deepcopy(result);broken['fpgas'][0]['part']='another-device'
    with pytest.raises(ValidationError):validate_multi_fpga_physical_report(broken)


@pytest.mark.parametrize('kwargs',[
    {'backend':'retired'},{'workers':0},{'workers':True},{'seed':-1},
    {'fpga_ids':[]},{'fpga_ids':'fpga0'},{'fpga_ids':['fpga0','fpga0']},
    {'original_ir_path':Path('original')},{'logic_path_database_path':Path('paths')},
    {'backend':'rapidwright'},
])
def test_invalid_public_request_is_rejected_before_tool_or_source_access(tmp_path,kwargs):
    options={'backend':'vivado',**kwargs}
    with patch('emuflow.flow.multi_fpga_physical_flow.resolve_native_executable',side_effect=AssertionError('premature tool lookup')):
        with pytest.raises(ValidationError):run_multi_fpga_physical_flow(tmp_path/'split',tmp_path/'board',tmp_path/'schedule',tmp_path/'output',**options)
    assert not (tmp_path/'output').exists()
