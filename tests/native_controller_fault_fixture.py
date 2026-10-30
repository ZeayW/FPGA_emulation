"""Inject worker faults into the real core controller, without a phase adapter."""
import json
from pathlib import Path
import sys
from tests.native_build import emuflow_native,native_pipeline_api_test


def install(request,body):
    """The API driver substitutes only the executable for native phase workers.

    Worker input generations remain bound by the genuine controller. A worker
    which tries to rewrite controller state must fail at that earlier boundary.
    All unmodified commands exec the genuine unified native executable.
    """
    root=Path(request).parent
    worker=root/'fault-worker'
    header='#!'+sys.executable+'\nimport json,os,sys,signal\nfrom pathlib import Path\n'
    header+='sys.path.insert(0,'+repr(str(Path(__file__).resolve().parents[1]))+')\n'
    header+='sys.path.insert(0,'+repr(str(Path(__file__).resolve().parents[1]/'src/python'))+')\n'
    header+='from tests.native_compile_state_fixture import _pack,_unpack,CompileContext\n'
    header+='def cancel(signum,frame):raise KeyboardInterrupt("worker cancelled")\nfor sig in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP):signal.signal(sig,cancel)\n'
    header+='if "phase1" in sys.argv:\n'
    header+=' receipt=Path(sys.argv[sys.argv.index("--report")+1]); previous=receipt.parents[2]/"preflight.json"\n'
    header+=' value=json.loads(previous.read_text()); before=value["state"]; ctx=CompileContext(_unpack(before))\n'
    header+='\n'.join(' '+line for line in body.splitlines())+'\n'
    header+=' after=_pack(vars(ctx))\n if after!=before:\n  value["state"]=after;previous.write_text(json.dumps(value))\n'
    header+='os.execv('+repr(str(emuflow_native()))+',['+repr(str(emuflow_native()))+',*sys.argv[1:]])\n'
    worker.write_text(header);worker.chmod(0o700)
    return [str(native_pipeline_api_test()),'compile-with-worker',str(request),str(worker)]
