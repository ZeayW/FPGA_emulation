"""Controlled remaining-adapter faults after the genuine native common owner."""
import json
from pathlib import Path


def install(request,body):
    from emuflow.common.paths import PACKAGE_ROOT
    value=json.loads(request.read_text());root=request.parent
    package=root/'source/emuflow';flow=package/'flow';flow.mkdir(parents=True,exist_ok=True)
    (package/'__init__.py').write_text('__path__.append('+repr(str(PACKAGE_ROOT))+')\n')
    (flow/'__init__.py').write_text('__path__.append('+repr(str(PACKAGE_ROOT/'flow'))+')\n')
    (flow/'native_compile_adapter.py').write_text('import runpy\nrunpy.run_path('+repr(str(PACKAGE_ROOT/'flow/native_compile_adapter.py'))+',run_name="__main__")\n')
    (flow/'multi_fpga_flow.py').write_text('from pathlib import Path\nimport os,sys\n'
        'class _CompileContext:\n def __init__(self,values):self.__dict__.update(values)\n'
        'def _compile_chimew_admission(ctx):\n'+''.join('    '+line+'\n' for line in body.splitlines()))
    architecture=root/'controlled-architecture.json';architecture.write_text('{}')
    options=value['options'];options.update(phase6_provider='chimew',physical=True,
        physical_backend='vivado',physical_architecture=str(architecture),global_sta_executable=options['opensta'])
    value['python_source']=str(package.parent)
    request.write_text(json.dumps(value))


def admitted(case,root,backend):
    """Real native common preparation followed by real device admission."""
    from tests.test_compile_preflight_native import request_options,run
    from emuflow.flow.native_compile import _request
    from emuflow.flow.native_compile_adapter import execute
    from emuflow.platform.default_platform import research_platform_path
    root.mkdir()
    options=request_options(root,backend)
    options.update(platform_path=research_platform_path(2),phase6_provider='chimew',physical_architecture=case[2])
    value,_=_request((),options)
    result,previous,operation=run(root,value)
    assert result.returncode==0,operation['error']
    output=root/'chimew.json'
    execute('chimew-admission',root/'request.json',previous,output,'test')
    return root/'request.json',output
