"""Exercise native admission directly; no phase adapter substitutes its output."""
import json
from pathlib import Path
import subprocess
import tempfile
from emuflow.flow.native_compile import _request
from tests.native_compile_state_fixture import _unpack
from tests.native_compile_state_fixture import CompileContext as _CompileContext
from emuflow.common.errors import ValidationError
from tests.native_build import emuflow_native


def preflight(*args, **kwargs):
    from tests.xilinx_timing_fixture import write_mock_xilinx_timing_contract
    with tempfile.TemporaryDirectory(prefix='preflight-contract-') as directory:
        root=Path(directory)
        if kwargs.get('architecture_timing_db') is None:
            kwargs['architecture_timing_db']=write_mock_xilinx_timing_contract(root/'timing.json')
        value,text=_request(args,kwargs)
        source=root/'request.json';source.write_text(text);state=root/'state.json';proof=root/'operation.json'
        result=subprocess.run([str(emuflow_native()),'--report',str(proof),'compile','prepare',str(source),str(state),'test'],capture_output=True,text=True,timeout=60)
        if result.returncode:
            operation=json.loads(proof.read_text()) if proof.exists() else json.loads(result.stderr.split('emuflow-native: ',1)[1])
            raise ValidationError(operation['error']['message'])
        ctx=_CompileContext(_unpack(json.loads(state.read_text())['state']))
        return ctx
