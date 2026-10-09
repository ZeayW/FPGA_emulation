from __future__ import annotations

import json
import gzip
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .errors import EmuFlowError
from .native_tools import resolve_native_executable
from .process_output import run_with_bounded_output
from .xilinx_primitives import (
    XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
    normalize_xilinx_mapped_json,
)


VALID_XILINX_FAMILIES = {"xcup", "xcu", "xc7"}
VALID_SYNTHESIS_POLICIES = {"native", "logic-only"}
YOSYS_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
YOSYS_DEFINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:=[^\s;]+)?$")
YOSYS_INCLUDE_DIR = re.compile(r"^[A-Za-z0-9_./:+-]+$")
LOGIC_ONLY_MAP = (
    Path(__file__).resolve().parents[2] / "scripts" / "yosys" / "logic_only_map.v"
)


def build_scalable_xilinx_yosys_script(
    sources: Iterable[Path],
    top: str,
    output: Path,
    *,
    include_dirs: Iterable[Path] = (),
    defines: Iterable[str] = (),
    stream_json: bool = False,
) -> str:
    """Build the hierarchical, native-Xilinx large-design mapping script.

    ``synth_xilinx`` is the reference mapping recipe, but its repeated global
    optimization passes become the dominant cost on million-cell hierarchical
    designs before Phase 3 has divided the design.  This recipe preserves the
    same public UltraScale+ primitive namespace while doing the expensive
    lowering in the order that scales for a partitioning frontend:

    * elaborate and normalize RTL while hierarchy is still compact;
    * map DSP and block/UltraRAM before flattening;
    * flatten only after stateful hard blocks are compact primitives; and
    * use one LUT6 ABC pass over the resulting flat combinational graph.

    It deliberately does not use VTR models, memory modes, or cell names.
    Every output is still audited by ``xilinx-ultrascaleplus-open-v1`` before
    it can enter Phase 1.  The caller must qualify equivalence and resource
    parity against the reference mapper before promoting this recipe.
    """

    source_list = list(sources)
    if not source_list:
        raise EmuFlowError("synthesis requires at least one RTL source")
    top_identifier = _yosys_identifier(top)
    read_options = [
        *(_yosys_include_dir(path) for path in include_dirs),
        *(f"-D{_yosys_define(value)}" for value in defines),
    ]
    read_sources = " ".join(_yosys_quote(str(path)) for path in source_list)
    write_json = "write_json -no-hidden-netnames -no-source-attributes"
    if not stream_json:
        write_json += f" {_yosys_quote(str(output))}"

    commands = [
        " ".join(["read_verilog", "-sv", *read_options, read_sources]),
        "read_verilog -lib -specify +/xilinx/cells_sim.v",
        "read_verilog -lib +/xilinx/cells_xtra.v",
        f"hierarchy -check -top {top_identifier}",
        # This is Yosys' architecture-neutral coarse synthesis boundary.  It
        # keeps memories and multipliers available for the native Xilinx maps
        # below and, unlike a VTR recipe, imports no foreign architecture.
        # Stop before generic ``synth`` enters its ``fine`` label.  ``fine``
        # begins with ``memory_map`` and would lower every still-generic
        # memory into soft logic before the Xilinx BRAM/URAM libraries below
        # can see it.  Keep sharing disabled here as well because it is run
        # once, deliberately, after DSP extraction.
        f"synth -top {top_identifier} -run begin:coarse -noalumacc -noshare",
        "memory_dff",
        "alumacc -macc-only",
        "maccmap -unmap",
        "techmap -map +/mul2dsp.v -map +/xilinx/xcu_dsp_map.v "
        "-D DSP_A_MAXWIDTH=27 -D DSP_B_MAXWIDTH=18 "
        "-D DSP_A_MAXWIDTH_PARTIAL=18 -D DSP_A_MINWIDTH=2 "
        "-D DSP_B_MINWIDTH=2 -D DSP_Y_MINWIDTH=9 "
        "-D DSP_SIGNEDONLY=1 -D DSP_NAME=$__MUL27X18",
        "select a:mul2dsp",
        "setattr -unset mul2dsp",
        "opt_expr -fine",
        "wreduce",
        "select -clear",
        "xilinx_dsp -family xcup",
        "chtype -set $mul t:$__soft_mul",
        "techmap -map +/cmp2lut.v -map +/cmp2lcu.v -D LUT_WIDTH=6",
        "alumacc",
        "share",
        "memory -nomap",
        "memory_libmap -logic-cost-rom 0.015625 "
        "-lib +/xilinx/lutrams_xcu.txt -lib +/xilinx/brams_xc4v.txt "
        "-D HAS_SIZE_36 -D HAS_MIXWIDTH_SDP -D HAS_ADDRCE "
        "-lib +/xilinx/urams.txt -no-auto-distributed",
        "techmap -map +/xilinx/lutrams_xc5v_map.v",
        "techmap -map +/xilinx/brams_xcu_map.v",
        "techmap -map +/xilinx/urams_map.v",
        "memory_map",
        # Stateful and arithmetic hard blocks are compact now.  Flattening at
        # this boundary avoids duplicating high-level memory/process state.
        "flatten",
        "techmap -map +/techmap.v -D LUT_SIZE=6 -map +/xilinx/arith_map.v",
        "techmap -map +/techmap.v -map +/xilinx/cells_map.v",
        "dfflegalize -cell $_DFFE_?P?P_ 01 -cell $_SDFFE_?P?P_ 01 "
        "-cell $_DLATCH_?P?_ 01",
        "techmap -map +/xilinx/ff_map.v",
        "opt_expr -mux_undef -noclkinv",
        "abc -lut 6",
        "techmap -map +/xilinx/lut_map.v -map +/xilinx/cells_map.v "
        "-D LUT_WIDTH=6",
        "clean",
        "check",
        "blackbox =A:whitebox",
        "delete t:$scopeinfo",
        'setattr -set KEEP "yes" c:*',
        'setattr -set DONT_TOUCH "yes" c:*',
        write_json,
    ]
    return "; ".join(commands)


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
    stream_json: bool = False,
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
        *(_yosys_include_dir(path) for path in include_list),
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
        # Stop before synth_xilinx's final ``check`` label.  That label also
        # runs ``stat -tech xilinx``; on large hierarchical designs the
        # diagnostic hierarchy tree can consume tens of GiB even though it
        # does not alter the mapped netlist.  The explicit post-flatten check
        # below remains the authoritative structural validation.
        synth_options.extend(
            ["-uram", "-nolutram", "-nosrl", "-run begin:check"]
        )
    post_mapping = []
    if policy == "logic-only":
        post_mapping.append(
            f"techmap -map {_yosys_quote(str(LOGIC_ONLY_MAP))}"
        )
    # ``synth_xilinx`` has already optimized every retained mapped primitive.
    # Running ``opt_clean`` after flattening a large Xilinx design rebuilds a
    # global SigPool and walks millions of mapped signals merely to remove
    # debug/unused objects.  It is not a technology-mapping or correctness
    # step and can take longer than synthesis while consuming tens of GiB.
    # Keep the structural ``check`` below and let the audited importer reject
    # unsupported or malformed primitives; skip only this redundant cleanup
    # for the exact Route A profile.
    post_flatten_cleanup = (
        []
        if mapping_profile == XILINX_ULTRASCALEPLUS_OPEN_PROFILE
        else ["opt_clean"]
    )
    commands = [
        " ".join(["read_verilog", "-sv", *read_options, read_sources]),
        f"hierarchy -check -top {top_identifier}",
        " ".join(synth_options),
        # The skipped synth_xilinx check label normally performs this
        # conversion after validating the mapped design.  Preserve it so the
        # JSON backend treats Xilinx library whiteboxes as leaf primitives.
        *(
            ["blackbox =A:whitebox"]
            if mapping_profile == XILINX_ULTRASCALEPLUS_OPEN_PROFILE
            else []
        ),
        # synth_xilinx preserves hierarchy in some Yosys releases. EmuIR
        # currently imports one module, so flatten the already mapped
        # primitives explicitly before writing the interchange JSON.
        "flatten",
        *post_mapping,
        *post_flatten_cleanup,
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
        *(
            [
                "write_json -no-hidden-netnames -no-source-attributes"
                + (
                    ""
                    if stream_json
                    else f" {_yosys_quote(str(output))}"
                )
            ]
            if mapping_profile == XILINX_ULTRASCALEPLUS_OPEN_PROFILE
            else [f"write_json {_yosys_quote(str(output))}"]
        ),
    ]
    if verilog_output is not None:
        commands.append(
            "write_verilog -norename "
            f"{_yosys_quote(str(verilog_output))}"
        )
    return "; ".join(commands)


def _run_yosys_streaming_gzip(
    command: list[str],
    output: Path,
    log_path: Optional[Path],
) -> subprocess.CompletedProcess[str]:
    """Stream a JSON-only Yosys stdout directly into deterministic gzip.

    Large mapped designs can produce several-GiB JSON documents.  Creating an
    uncompressed checkpoint before parsing it is unnecessary I/O and can
    exhaust a shared filesystem quota.  Yosys runs in quiet mode, so stdout is
    reserved for the backend document and stderr remains a bounded diagnostic
    artifact.
    """

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    diagnostic = output.with_name(
        f".{output.name}.{uuid.uuid4().hex}.stderr.tmp"
    )
    process: Optional[subprocess.Popen[bytes]] = None
    try:
        with diagnostic.open("wb") as errors:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=errors,
            )
            if process.stdout is None:  # pragma: no cover - guaranteed by PIPE
                raise AssertionError("Yosys stdout pipe was not created")
            with temporary.open("wb") as raw:
                with gzip.GzipFile(
                    filename="",
                    mode="wb",
                    compresslevel=1,
                    fileobj=raw,
                    mtime=0,
                ) as compressed:
                    for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
                        compressed.write(chunk)
                raw.flush()
                os.fsync(raw.fileno())
            return_code = process.wait()
        diagnostic_bytes = diagnostic.read_bytes()
        maximum = 2 * 1024 * 1024
        diagnostic_text = diagnostic_bytes[-maximum:].decode(
            "utf-8", errors="replace"
        )
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(diagnostic_text, encoding="utf-8")
        if return_code == 0:
            os.replace(temporary, output)
        else:
            temporary.unlink(missing_ok=True)
        return subprocess.CompletedProcess(
            command,
            return_code,
            stdout=diagnostic_text,
        )
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        temporary.unlink(missing_ok=True)
        diagnostic.unlink(missing_ok=True)


def build_generic_yosys_script(
    sources: Iterable[Path],
    top: str,
    output: Path,
    include_dirs: Iterable[Path] = (),
    defines: Iterable[str] = (),
) -> str:
    """Build an architecture-neutral LUT6/FF synthesis script for EmuIR."""

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
        f"write_json {_yosys_quote(str(output))}",
    ]
    return "; ".join(commands)


def run_generic_yosys(
    sources: Iterable[Path],
    top: str,
    output: Path,
    executable: Optional[str] = None,
    log_path: Optional[Path] = None,
    include_dirs: Iterable[Path] = (),
    defines: Iterable[str] = (),
) -> None:
    """Synthesize RTL to provider-neutral LUT6/FF Yosys JSON."""

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
    script = build_generic_yosys_script(
        source_list,
        top,
        output,
        include_dirs=include_list,
        defines=define_list,
    )
    completed = run_with_bounded_output([command, "-q", "-p", script])
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
        stream_json=output.suffix == ".gz",
    )
    if output.suffix == ".gz":
        completed = _run_yosys_streaming_gzip(
            [command, "-q", "-p", script], output, log_path
        )
    else:
        completed = run_with_bounded_output([command, "-q", "-p", script])
    if log_path is not None and output.suffix != ".gz":
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

    raw_output = output.with_name(
        f".{output.name}.pre-normalize"
        + (".json.gz" if output.suffix == ".gz" else "")
    )
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


def run_scalable_xilinx_ultrascaleplus_yosys(
    sources: Iterable[Path],
    top: str,
    output: Path,
    *,
    executable: Optional[str] = None,
    log_path: Optional[Path] = None,
    include_dirs: Iterable[Path] = (),
    defines: Iterable[str] = (),
) -> Dict[str, Any]:
    """Run and audit the candidate scalable native-Xilinx mapper.

    This entry point is intentionally separate from the production mapper
    while runtime, primitive inventory, functional equivalence, and physical
    QoR are being compared.  Both routes produce the exact same externally
    declared mapping profile; the provider field records which implementation
    produced the candidate so experimental results cannot be confused.
    """

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
    raw_output = output.with_name(
        f".{output.name}.scalable-pre-normalize"
        + (".json.gz" if output.suffix == ".gz" else "")
    )
    raw_output.parent.mkdir(parents=True, exist_ok=True)
    script = build_scalable_xilinx_yosys_script(
        source_list,
        top,
        raw_output,
        include_dirs=include_list,
        defines=define_list,
        stream_json=raw_output.suffix == ".gz",
    )
    try:
        if raw_output.suffix == ".gz":
            completed = _run_yosys_streaming_gzip(
                [command, "-q", "-p", script], raw_output, log_path
            )
        else:
            completed = run_with_bounded_output([command, "-q", "-p", script])
            if log_path is not None:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text(completed.stdout, encoding="utf-8")
        if completed.returncode != 0:
            tail = "\n".join(completed.stdout.splitlines()[-20:])
            raise EmuFlowError(
                "scalable Xilinx synthesis failed with exit code "
                f"{completed.returncode}\n{tail}"
            )
        if not raw_output.is_file():
            raise EmuFlowError(
                "scalable Xilinx synthesis reported success but did not "
                f"create expected output: {raw_output}"
            )
        normalization = normalize_xilinx_mapped_json(
            raw_output, output, top=top
        )
    finally:
        raw_output.unlink(missing_ok=True)
    return {
        "status": "pass",
        "provider": "yosys-native-xilinx-hierarchical-v1",
        "family": "xcup",
        "policy": "native",
        "mapping_profile": XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
        "normalization": normalization,
        "primitive_audit": normalization["primitive_audit"],
    }
