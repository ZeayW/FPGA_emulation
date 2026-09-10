from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Iterable, Optional

from .errors import EmuFlowError
from .native_tools import resolve_native_executable


VALID_XILINX_FAMILIES = {"xcup", "xcu", "xc7"}
VALID_SYNTHESIS_POLICIES = {"native", "logic-only"}
YOSYS_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
YOSYS_DEFINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:=[^\s;]+)?$")
LOGIC_ONLY_MAP = (
    Path(__file__).resolve().parents[2] / "scripts" / "yosys" / "logic_only_map.v"
)


def _run_logged_yosys(command: list[str], log_path: Optional[Path]):
    """Stream a requested scratch log; retain only a bounded failure tail."""
    if log_path is None:
        result = subprocess.run(command, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, check=False)
        return result.returncode, result.stdout[-65536:]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                check=False)
    if result.returncode == 0:
        return 0, ""
    with log_path.open("rb") as log:
        log.seek(0, 2)
        log.seek(max(0, log.tell() - 65536))
        tail = log.read().decode("utf-8", errors="replace")
    return result.returncode, tail


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


def build_yosys_script(
    sources: Iterable[Path],
    top: str,
    output: Path,
    family: str = "xcup",
    policy: str = "native",
    verilog_output: Optional[Path] = None,
    include_dirs: Iterable[Path] = (),
    defines: Iterable[str] = (),
) -> str:
    source_list = list(sources)
    if not source_list:
        raise EmuFlowError("synthesis requires at least one RTL source")
    if family not in VALID_XILINX_FAMILIES:
        raise EmuFlowError(
            f"unsupported Xilinx family {family!r}; "
            f"expected one of {sorted(VALID_XILINX_FAMILIES)}"
        )
    if policy not in VALID_SYNTHESIS_POLICIES:
        raise EmuFlowError(
            f"unsupported synthesis policy {policy!r}; "
            f"expected one of {sorted(VALID_SYNTHESIS_POLICIES)}"
        )
    top_identifier = _yosys_identifier(top)

    include_list = list(include_dirs)
    define_list = [_yosys_define(value) for value in defines]
    read_options = [
        *(f"-I{_yosys_quote(str(path))}" for path in include_list),
        *(f"-D{value}" for value in define_list),
    ]
    read_sources = " ".join(_yosys_quote(str(path)) for path in source_list)
    synth_options = [
        f"synth_xilinx -family {family}",
        f"-top {top_identifier}",
        "-noiopad",
        "-noclkbuf",
    ]
    if policy == "logic-only":
        synth_options.extend(
            [
                "-nocarry",
                "-nowidelut",
                "-nodsp",
                "-nobram",
                "-nolutram",
                "-nosrl",
            ]
        )
    post_mapping = []
    if policy == "logic-only":
        post_mapping.append(
            f"techmap -map {_yosys_quote(str(LOGIC_ONLY_MAP))}"
        )
    commands = [
        " ".join(["read_verilog", "-sv", *read_options, read_sources]),
        f"hierarchy -check -top {top_identifier}",
        " ".join(synth_options),
        # synth_xilinx preserves hierarchy in some Yosys releases. EmuIR
        # currently imports one module, so flatten the already mapped
        # primitives explicitly before writing the interchange JSON.
        "flatten",
        *post_mapping,
        "opt_clean",
        "check",
        # Yosys 0.57+ exports debug-only hierarchy metadata as $scopeinfo
        # cells by default. They are pinless and have no hardware behavior.
        "delete t:$scopeinfo",
        # Vivado otherwise re-optimizes some mapped primitives (for example,
        # redundant PicoRV32 register-file bits) and breaks the one-to-one
        # identity contract between EmuIR, OpenPARF, XDC, and the routed
        # design. Emit preservation attributes on every mapped cell.
        # Use Vivado's canonical uppercase, string-valued spellings. Numeric
        # lowercase attributes are preserved by Yosys but Vivado does not
        # honor them for constant-control FFs.
        'setattr -set KEEP "yes" c:*',
        'setattr -set DONT_TOUCH "yes" c:*',
        f"write_json {_yosys_quote(str(output))}",
    ]
    if verilog_output is not None:
        commands.append(
            "write_verilog -norename "
            f"{_yosys_quote(str(verilog_output))}"
        )
    return "; ".join(commands)


def build_generic_yosys_script(
    sources: Iterable[Path],
    top: str,
    output: Path,
    *, lut_size: int = 6,
) -> str:
    """Build portable LUT/FF mapping; ECP5 consumers explicitly request LUT4."""

    if type(lut_size) is not int or lut_size not in (4, 6):
        raise EmuFlowError("generic mapping supports explicit LUT4 or LUT6")

    source_list = list(sources)
    if not source_list:
        raise EmuFlowError("synthesis requires at least one RTL source")
    top_identifier = _yosys_identifier(top)
    read_sources = " ".join(_yosys_quote(str(path)) for path in source_list)
    commands = [
        f"read_verilog -sv {read_sources}",
        f"hierarchy -check -top {top_identifier}",
        "proc",
        "flatten",
        "opt",
        "memory_dff",
        "memory_map",
        "techmap",
        "opt",
        "dffunmap",
        f"abc -lut {lut_size}",
        "dffunmap",
        # Yosys 0.57+ may materialize debug-only hierarchy metadata as
        # $scopeinfo cells. They have no hardware behavior or pins and must
        # not enter the physical instance inventory.
        "delete t:$scopeinfo",
        "clean",
        "check",
        f"write_json {_yosys_quote(str(output))}",
    ]
    return "; ".join(commands)


def run_generic_yosys(
    sources: Iterable[Path],
    top: str,
    output: Path,
    executable: Optional[str] = None,
    log_path: Optional[Path] = None,
    *, lut_size: int = 6,
) -> None:
    """Synthesize RTL to explicitly selected provider-neutral LUT4/6 and FF JSON."""

    source_list = list(sources)
    for source in source_list:
        if not source.is_file():
            raise EmuFlowError(f"RTL source does not exist: {source}")
    command = resolve_native_executable("yosys", executable)
    output.parent.mkdir(parents=True, exist_ok=True)
    script = build_generic_yosys_script(source_list, top, output, lut_size=lut_size)
    returncode, output_tail = _run_logged_yosys([command, "-p", script], log_path)
    if returncode != 0:
        tail = "\n".join(output_tail.splitlines()[-20:])
        raise EmuFlowError(
            "generic Yosys synthesis failed with exit code "
            f"{returncode}\n{tail}"
        )
    if not output.is_file():
        raise EmuFlowError(
            f"Yosys reported success but did not create expected output: {output}"
        )


def run_yosys(
    sources: Iterable[Path],
    top: str,
    output: Path,
    family: str = "xcup",
    policy: str = "native",
    verilog_output: Optional[Path] = None,
    executable: Optional[str] = None,
    log_path: Optional[Path] = None,
    include_dirs: Iterable[Path] = (),
    defines: Iterable[str] = (),
) -> None:
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
    if verilog_output is not None:
        verilog_output.parent.mkdir(parents=True, exist_ok=True)
    script = build_yosys_script(
        source_list,
        top,
        output,
        family,
        policy,
        verilog_output=verilog_output,
        include_dirs=include_list,
        defines=define_list,
    )
    returncode, output_tail = _run_logged_yosys([command, "-p", script], log_path)
    if returncode != 0:
        tail = "\n".join(output_tail.splitlines()[-20:])
        raise EmuFlowError(
            f"Yosys synthesis failed with exit code {returncode}\n{tail}"
        )
    if not output.is_file():
        raise EmuFlowError(
            f"Yosys reported success but did not create expected output: {output}"
        )
    if verilog_output is not None and not verilog_output.is_file():
        raise EmuFlowError(
            "Yosys reported success but did not create expected mapped "
            f"Verilog: {verilog_output}"
        )
