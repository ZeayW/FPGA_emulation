from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .errors import EmuFlowError
from .native_tools import resolve_native_executable
from .xilinx_primitives import (
    XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
    normalize_xilinx_mapped_json,
)


VALID_XILINX_FAMILIES = {"xcup", "xcu", "xc7"}
VALID_SYNTHESIS_POLICIES = {"native", "logic-only"}
YOSYS_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
YOSYS_DEFINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:=[^\s;]+)?$")
LOGIC_ONLY_MAP = (
    Path(__file__).resolve().parents[2] / "scripts" / "yosys" / "logic_only_map.v"
)


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
    mapping_profile: Optional[str] = None,
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
    if mapping_profile is not None:
        if mapping_profile != XILINX_ULTRASCALEPLUS_OPEN_PROFILE:
            raise EmuFlowError(
                f"unsupported Xilinx mapping profile {mapping_profile!r}"
            )
        if family != "xcup" or policy != "native":
            raise EmuFlowError(
                f"{mapping_profile} requires family='xcup' and policy='native'"
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
    elif mapping_profile == XILINX_ULTRASCALEPLUS_OPEN_PROFILE:
        # Route A v1 retains the hard resources consumed by real designs.
        # Distributed RAM and SRLs are outside the v1 packer contract, so
        # lower those structures to audited LUT/FF primitives explicitly.
        synth_options.extend(["-uram", "-nolutram", "-nosrl"])
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
) -> str:
    """Build an architecture-neutral LUT6/FF synthesis script for EmuIR."""

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
        "abc -lut 6",
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
) -> None:
    """Synthesize RTL to provider-neutral LUT6/FF Yosys JSON."""

    source_list = list(sources)
    for source in source_list:
        if not source.is_file():
            raise EmuFlowError(f"RTL source does not exist: {source}")
    command = resolve_native_executable("yosys", executable)
    output.parent.mkdir(parents=True, exist_ok=True)
    script = build_generic_yosys_script(source_list, top, output)
    completed = subprocess.run(
        [command, "-p", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(completed.stdout, encoding="utf-8")
    if completed.returncode != 0:
        tail = "\n".join(completed.stdout.splitlines()[-20:])
        raise EmuFlowError(
            "generic Yosys synthesis failed with exit code "
            f"{completed.returncode}\n{tail}"
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
    mapping_profile: Optional[str] = None,
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
        mapping_profile=mapping_profile,
    )
    completed = subprocess.run(
        [command, "-p", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(completed.stdout, encoding="utf-8")
    if completed.returncode != 0:
        tail = "\n".join(completed.stdout.splitlines()[-20:])
        raise EmuFlowError(
            f"Yosys synthesis failed with exit code {completed.returncode}\n{tail}"
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


def run_xilinx_ultrascaleplus_yosys(
    sources: Iterable[Path],
    top: str,
    output: Path,
    *,
    executable: Optional[str] = None,
    log_path: Optional[Path] = None,
    include_dirs: Iterable[Path] = (),
    defines: Iterable[str] = (),
) -> Dict[str, Any]:
    """Run the explicit Route A mapping profile and audit every primitive."""

    raw_output = output.with_name(f".{output.name}.pre-normalize")
    try:
        run_yosys(
            sources,
            top,
            raw_output,
            family="xcup",
            policy="native",
            executable=executable,
            log_path=log_path,
            include_dirs=include_dirs,
            defines=defines,
            mapping_profile=XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
        )
        normalization = normalize_xilinx_mapped_json(
            raw_output, output, top=top
        )
    finally:
        raw_output.unlink(missing_ok=True)
    return {
        "status": "pass",
        "provider": "yosys-synth-xilinx",
        "family": "xcup",
        "policy": "native",
        "mapping_profile": XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
        "normalization": normalization,
        "primitive_audit": normalization["primitive_audit"],
    }
