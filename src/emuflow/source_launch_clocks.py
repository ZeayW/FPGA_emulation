"""Bounded control for the native original-source launch-clock producer.

The validated source emitter supplies model files and their existing seals.
Original endpoint resolution, physical query scans, tool execution and fact
publication remain in C++; no path population or endpoint graph enters Python.
"""
from pathlib import Path
import re
import subprocess
import tempfile

from .errors import EmuFlowError, ValidationError
from .native_protocol import CheckedRequestWriter
from .native_tools import native_file_generation, resolve_native_executable
from .tool_runtime import _request_bytes
from .store_publication import require_store_sources_unchanged


SOURCE_KEYS=('original_ir_sha256','path_database_sha256','routes_sha256',
             'schedule_sha256','semantic_sha256','timing_io_sha256')


def build_source_launch_clock_store(original_ir, output, *, logic_costs,
        local_costs, source_identity, command, environment, timing_inputs,
        source_bindings, executable=None):
    """Consume an already source-sealed Phase2 model and all physical stores.

    `timing_inputs` maps immutable model/script/engine paths to the hashes
    produced by their validated emitters/probes. It is bounded control data.
    `source_bindings` retains those parents' previously validated generations;
    taking a fresh snapshot cannot renew an older content seal.
    The emitted clock store owns each requested original integer coordinate
    once; the caller must bind it into the complete global timing context.
    """
    if set(source_identity)!=set(SOURCE_KEYS) or any(
            not isinstance(source_identity[key],str)or re.fullmatch('[0-9a-f]{64}',source_identity[key])is None
            for key in SOURCE_KEYS):
        raise ValidationError('source launch parent seals are invalid')
    if not 0<len(logic_costs)<=256 or len(local_costs)!=len(logic_costs)or not 8<=len(timing_inputs)<=16:
        raise ValidationError('source launch physical/model input coverage differs')
    timing={Path(path).resolve():seal for path,seal in timing_inputs.items()}
    if len(timing)!=len(timing_inputs)or any(not isinstance(seal,str)or re.fullmatch('[0-9a-f]{64}',seal)is None for seal in timing.values()):
        raise ValidationError('source launch timing seals are invalid or repeat')
    source=Path(original_ir).resolve();logic=[Path(path).resolve()for path in logic_costs];local=[Path(path).resolve()for path in local_costs]
    bindings=tuple(source_bindings);by_path={binding.path:binding for binding in bindings}
    if len(by_path)!=len(bindings)or set(by_path)!={source,*logic,*local,*timing}:
        raise ValidationError('source launch parent generation bindings are incomplete or repeat')
    require_store_sources_unchanged(bindings)
    native=resolve_native_executable('emuflow-native',executable)
    probe=subprocess.run([native,'check-runtime','source-launch-clock-store-v1'],capture_output=True,text=True,timeout=10)
    if probe.returncode:raise EmuFlowError('native source launch-clock producer is unavailable: '+probe.stderr)
    output=Path(output).resolve();output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.source-launch-',dir=output.parent)as directory:
        root=Path(directory);tool=root/'tool.bin';request=root/'request.bin'
        tool.write_bytes(_request_bytes(command,cwd=output.parent,environment=environment,
            forward_stdout=False,maximum_bytes=65536,timeout_ms=0,termination_grace_ms=1000,
            cpu_seconds=0,address_space_bytes=0,file_size_bytes=0))
        ordered=[source,tool,*logic,*local,*timing]
        writer=CheckedRequestWriter(request)
        try:
            writer.put('QIIII',0x3151424c534d5545,1,len(logic),len(local),len(timing))
            for key in SOURCE_KEYS:writer.text(source_identity[key])
            for path in ordered:writer.put('QQQqqqq',*(native_file_generation(tool)if path==tool else by_path[path].generation))
            for seal in timing.values():writer.text(seal)
            writer.finish()
        except BaseException:
            writer.close();raise
        result=subprocess.run([native,'phase7','build-launch-clocks',str(source),str(request),str(tool),
            *(str(path)for path in (*logic,*local,*timing)),str(output)],capture_output=True,text=True)
        if result.returncode:
            raise EmuFlowError('native source launch-clock production failed: '+result.stderr[-16384:])
    return output
