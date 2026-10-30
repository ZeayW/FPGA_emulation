"""Independent mapping statistics; no netlist JSON producer or flow control."""
from pathlib import Path
from typing import Any,Dict,Iterable,Optional
import json
import re
import time
from emuflow.common.errors import EmuFlowError
from emuflow.common.native_tools import resolve_native_executable
from emuflow.common.process_output import run_with_bounded_output
from emuflow.frontend.policy import (DEFAULT_XILINX_MAPPING_STRATEGY,
    VALID_XILINX_MAPPING_STRATEGIES,YOSYS_IDENTIFIER,YOSYS_DEFINE,YOSYS_INCLUDE_DIR)
from emuflow.physical.xilinx_primitives import XILINX_ULTRASCALEPLUS_OPEN_PROFILE

def _xilinx_mapping_strategy_options(strategy: str) -> list[str]:
    if strategy not in VALID_XILINX_MAPPING_STRATEGIES:
        raise EmuFlowError(
            f"unsupported Xilinx mapping strategy {strategy!r}; "
            f"expected one of {sorted(VALID_XILINX_MAPPING_STRATEGIES)}"
        )
    options: list[str] = []
    if strategy.startswith("flatten-"):
        options.append("-flatten")
    if "-abc9-" in strategy:
        options.append("-abc9")
    return options


def _yosys_quote(value: str) -> str:
    # Yosys accepts double-quoted strings with JSON-compatible escaping.
    return json.dumps(value)


def _yosys_identifier(value: str) -> str:
    if not YOSYS_IDENTIFIER.fullmatch(value):
        raise EmuFlowError(
            f"unsupported Yosys identifier {value!r}; "
            "expected a simple Verilog module name"
        )
    return value


def _yosys_define(value: str) -> str:
    if not YOSYS_DEFINE.fullmatch(value):
        raise EmuFlowError(
            f"unsupported Yosys define {value!r}; expected NAME or NAME=VALUE"
        )
    return value


def _yosys_include_dir(value: Path) -> str:
    """Return the exact ``-Idir`` token accepted by ``read_verilog``.

    Yosys treats ``-I\"dir\"`` as a directory whose name contains literal
    quote characters.  Quoting the complete token instead makes it a source
    filename.  Therefore include directories must use the documented compact
    form and are restricted to a shell-independent path alphabet.  This fails
    closed for whitespace or command separators instead of producing a script
    that is either unsafe or silently unable to locate headers.
    """

    raw = str(value)
    if not YOSYS_INCLUDE_DIR.fullmatch(raw):
        raise EmuFlowError(
            f"unsupported Yosys include directory {raw!r}; "
            "paths must not contain whitespace or command separators"
        )
    return f"-I{raw}"


def build_xilinx_mapping_statistics_script(
    sources: Iterable[Path],
    top: str,
    output: Path,
    *,
    include_dirs: Iterable[Path] = (),
    defines: Iterable[str] = (),
    mapping_strategy: str = DEFAULT_XILINX_MAPPING_STRATEGY,
) -> str:
    """Build a compact Route A mapping qualification script.

    This runs the same technology mapper as production but writes only Yosys'
    primitive-count statistics.  It deliberately avoids materializing the
    multi-GiB mapped JSON used by the full flow, so alternative mapping
    strategies can be compared without turning diagnostic data into a hot-path
    or storage burden.
    """

    source_list = list(sources)
    if not source_list:
        raise EmuFlowError("synthesis requires at least one RTL source")
    strategy_options = _xilinx_mapping_strategy_options(mapping_strategy)
    top_identifier = _yosys_identifier(top)
    read_options = [
        *(_yosys_include_dir(path) for path in include_dirs),
        *(f"-D{_yosys_define(value)}" for value in defines),
    ]
    read_sources = " ".join(_yosys_quote(str(path)) for path in source_list)
    synth_options = [
        "synth_xilinx -family xcup",
        f"-top {top_identifier}",
        "-noiopad",
        "-noclkbuf",
        "-uram",
        "-nolutram",
        "-nosrl",
        "-run begin:check",
    ]
    synth_options.extend(strategy_options)
    commands = [
        " ".join(["read_verilog", "-sv", *read_options, read_sources]),
        f"hierarchy -check -top {top_identifier}",
        " ".join(synth_options),
        "blackbox =A:whitebox",
        "flatten",
        "check",
        "delete t:$scopeinfo",
        "tee -q -o "
        f"{_yosys_quote(str(output))} stat -json -top {top_identifier}",
    ]
    return "; ".join(commands)


def run_xilinx_mapping_statistics(
    sources: Iterable[Path],
    top: str,
    output: Path,
    *,
    executable: Optional[str] = None,
    log_path: Optional[Path] = None,
    include_dirs: Iterable[Path] = (),
    defines: Iterable[str] = (),
    mapping_strategy: str = DEFAULT_XILINX_MAPPING_STRATEGY,
    profile_passes: bool = False,
) -> Dict[str, Any]:
    """Run one bounded mapping qualification and return compact statistics."""

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
    define_list = list(defines)
    command = resolve_native_executable("yosys", executable)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    script = build_xilinx_mapping_statistics_script(
        source_list,
        top,
        output,
        include_dirs=include_list,
        defines=define_list,
        mapping_strategy=mapping_strategy,
    )
    started = time.monotonic()
    # Qualification only: headers plus the native detailed timing footer.
    # Do not enable verbose full logs or diagnostic netlist dumps.
    # -t prefixes tee's stat JSON too, corrupting the statistics protocol.
    flags = ["-v", "2", "-d"] if profile_passes else ["-q"]
    completed = run_with_bounded_output([command, *flags, "-p", script])
    elapsed_seconds = time.monotonic() - started
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(completed.stdout, encoding="utf-8")
    if completed.returncode != 0:
        output.unlink(missing_ok=True)
        tail = "\n".join(completed.stdout.splitlines()[-20:])
        raise EmuFlowError(
            "Xilinx mapping qualification failed with exit code "
            f"{completed.returncode}\n{tail}"
        )
    if not output.is_file():
        raise EmuFlowError(
            "Yosys mapping qualification did not create statistics: "
            f"{output}"
        )
    statistics = json.loads(output.read_text(encoding="utf-8"))
    if not isinstance(statistics, dict) or not isinstance(
        statistics.get("modules"), dict
    ):
        raise EmuFlowError("Yosys mapping statistics are malformed")
    pass_timings = []
    if profile_passes:
        for match in re.finditer(
            r"^\s*(\d+)%\s+(\d+) calls\s+([0-9.]+) sec\s+(\S+)\s*$",
            completed.stdout,
            re.MULTILINE,
        ):
            pass_timings.append({
                "pass": match[4], "calls": int(match[2]),
                "seconds": float(match[3]), "percent": int(match[1]),
            })
        if not pass_timings:
            raise EmuFlowError("Yosys did not emit the requested detailed pass timings")
    return {
        "status": "pass",
        "schema": "emuflow.xilinx-mapping-qualification/v1",
        "mapping_profile": XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
        "mapping_strategy": mapping_strategy,
        "top": top,
        "elapsed_seconds": elapsed_seconds,
        **({"pass_timings": pass_timings} if profile_passes else {}),
        "statistics": statistics,
    }
