"""Independent synthesis-script oracle for native configuration differential tests.

This module contains no engine launcher and is never installed.
"""
from pathlib import Path
from typing import Iterable,Optional
from emuflow.common.paths import REPOSITORY_ROOT
from emuflow.common.errors import EmuFlowError
from emuflow.frontend.policy import (VALID_XILINX_FAMILIES,VALID_SYNTHESIS_POLICIES,
    DEFAULT_XILINX_MAPPING_STRATEGY,VALID_XILINX_MAPPING_STRATEGIES)
from emuflow_tools.yosys_mapping_statistics import (
    _xilinx_mapping_strategy_options,_yosys_quote,_yosys_identifier,_yosys_define,_yosys_include_dir)
from emuflow.physical.xilinx_primitives import XILINX_ULTRASCALEPLUS_OPEN_PROFILE
LOGIC_ONLY_MAP=REPOSITORY_ROOT/'scripts/yosys/logic_only_map.v'

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
    mapping_strategy: Optional[str] = None,
    stream_json: bool = False,
    hierarchical_interchange: bool = False,
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
    if mapping_strategy is not None:
        if mapping_profile != XILINX_ULTRASCALEPLUS_OPEN_PROFILE:
            raise EmuFlowError(
                "an explicit Xilinx mapping strategy requires the "
                f"{XILINX_ULTRASCALEPLUS_OPEN_PROFILE!r} profile"
            )
        _xilinx_mapping_strategy_options(mapping_strategy)
    if hierarchical_interchange and not (
        stream_json
        and mapping_profile == XILINX_ULTRASCALEPLUS_OPEN_PROFILE
    ):
        raise EmuFlowError(
            "hierarchical Yosys interchange requires the streaming "
            f"{XILINX_ULTRASCALEPLUS_OPEN_PROFILE!r} profile"
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
        selected_strategy = (
            mapping_strategy or DEFAULT_XILINX_MAPPING_STRATEGY
        )
        synth_options.extend(
            _xilinx_mapping_strategy_options(selected_strategy)
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
        # The production Xilinx path preserves mapped module hierarchy and
        # expands it record-by-record in the indexed Phase 1 importer.  This
        # avoids constructing a second, fully flattened multi-million-cell
        # RTLIL design inside Yosys.  File-based/legacy consumers still get
        # the historical flattened interchange explicitly.
        *([] if hierarchical_interchange else ["flatten"]),
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
                # The pinned production Yosys 0.57 backend supports
                # ``-noscopeinfo`` but not the newer
                # ``-no-hidden-netnames``/``-no-source-attributes`` flags.
                # Phase 1 consumes this stream incrementally, so retaining
                # those fields does not recreate the old graph-sized JSON
                # artifact or a second in-memory design.
                "write_json -noscopeinfo"
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
