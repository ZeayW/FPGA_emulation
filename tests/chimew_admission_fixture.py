"""Complete native compile admission with source-qualified device geometry."""
import json
from pathlib import Path


def admitted(case,root,backend):
    """Real native common preparation followed by real device admission."""
    from tests.test_compile_preflight_native import request_options,run
    from emuflow.flow.native_compile import _request
    from emuflow.platform.default_platform import research_platform_path
    root.mkdir()
    options=request_options(root,backend)
    options.update(platform_path=research_platform_path(len(case[0].fpgas)),phase6_provider='chimew',physical_architecture=case[2])
    value,_=_request((),options)
    result,previous,operation=run(root,value)
    assert result.returncode==0,operation['error']
    return root/'request.json',previous
