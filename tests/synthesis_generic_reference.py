"""Independent generic synthesis script/stream fixture; not installed."""
from pathlib import Path
from typing import Iterable,Callable,TextIO,TypeVar,Optional
from emuflow.common.errors import EmuFlowError
from emuflow.common.native_tools import resolve_native_executable
from emuflow.frontend.synthesis import _yosys_identifier, _yosys_include_dir, _yosys_define, _yosys_quote, _run_yosys_json_consumer
_T=TypeVar('_T')

def _build_generic_yosys_stream_script(
    sources: Iterable[Path],
    top: str,
    include_dirs: Iterable[Path] = (),
    defines: Iterable[str] = (),
) -> str:
    """Build the internal LUT6/FF stream used by VTR transport synthesis."""

    source_list = list(sources)
    if not source_list:
        raise EmuFlowError("synthesis requires at least one RTL source")
    top_identifier = _yosys_identifier(top)
    read_options = [
        *(_yosys_include_dir(path) for path in include_dirs),
        *(f"-D{_yosys_define(value)}" for value in defines),
    ]
    read_sources = " ".join(_yosys_quote(str(path)) for path in source_list)
    commands = [
        " ".join(["read_verilog", "-sv", *read_options, read_sources]),
        f"hierarchy -check -top {top_identifier}",
        "proc",
        "flatten",
        "opt",
        "memory_dff",
        "memory_map",
        "techmap",
        "opt",
        "dffunmap",
        "abc -lut 6",
        "dffunmap",
        # Yosys 0.57+ may materialize debug-only hierarchy metadata as
        # $scopeinfo cells. They have no hardware behavior or pins and must
        # not enter the physical instance inventory.
        "delete t:$scopeinfo",
        "clean",
        "check",
        "write_json",
    ]
    return "; ".join(commands)


def run_generic_yosys_stream(
    sources: Iterable[Path],
    top: str,
    consumer: Callable[[TextIO], _T],
    *,
    executable: Optional[str] = None,
    log_path: Optional[Path] = None,
    include_dirs: Iterable[Path] = (),
    defines: Iterable[str] = (),
) -> _T:
    """Pipe generic LUT6/FF JSON to a bounded indexed consumer."""

    source_list = list(sources)
    for source in source_list:
        if not source.is_file():
            raise EmuFlowError(f"RTL source does not exist: {source}")
    include_list = list(include_dirs)
    for include_dir in include_list:
        if not include_dir.is_dir():
            raise EmuFlowError(
                f"Verilog include directory does not exist: {include_dir}"
            )
    command = resolve_native_executable("yosys", executable)
    script = _build_generic_yosys_stream_script(
        source_list,
        top,
        include_dirs=include_list,
        defines=list(defines),
    )
    return _run_yosys_json_consumer(
        [command, "-q", "-p", script],
        consumer,
        log_path.parent if log_path is not None else source_list[0].parent,
        log_path,
        "generic Yosys synthesis",
    )
