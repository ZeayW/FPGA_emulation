#!/usr/bin/env python3

"""Generate FPGA-safe declarations or models for NVDLA ASIC SRAM wrappers."""

import argparse
import re
from pathlib import Path
from typing import Dict, List, Tuple


MODULE = re.compile(
    r"\bmodule\s+(nv_ram_[A-Za-z0-9_]+)\s*\((.*?)\)\s*;",
    re.DOTALL,
)
PARAMETER = re.compile(
    r"^\s*parameter\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^;]+);",
    re.MULTILINE,
)
PORT = re.compile(
    r"^\s*(input|output|inout)\s+"
    r"(?:(?:wire|reg|logic)\s+)?"
    r"(\[[^;\n]+\])?\s*"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*;",
    re.MULTILINE,
)


def _parse_wrapper(path: Path) -> Tuple[str, str, List[Tuple[str, str, str]]]:
    text = path.read_text(encoding="utf-8")
    module_match = MODULE.search(text)
    if module_match is None:
        raise ValueError(f"{path}: expected one nv_ram_* module declaration")
    name = module_match.group(1)
    header_ports = [
        token.strip()
        for token in module_match.group(2).replace("\n", " ").split(",")
        if token.strip()
    ]
    parameter_match = PARAMETER.search(text)
    if parameter_match is None:
        raise ValueError(f"{path}: expected an SRAM wrapper parameter")
    parameter = (
        f"parameter {parameter_match.group(1)}="
        f"{parameter_match.group(2).strip()}"
    )
    declarations: Dict[str, Tuple[str, str, str]] = {}
    for direction, width, port_name in PORT.findall(text):
        declarations[port_name] = (direction, width.strip(), port_name)
    missing = [port_name for port_name in header_ports if port_name not in declarations]
    if missing:
        raise ValueError(f"{path}: missing port declarations for {missing}")
    return name, parameter, [declarations[port_name] for port_name in header_ports]


BLACKBOX_MEMORY_POLICY = "interface-accurate-black-box-scale-abstraction"
PHYSICAL_MEMORY_POLICY = "physically-implementable-shared-memory-model-v2"
MEMORY_POLICIES = {BLACKBOX_MEMORY_POLICY, PHYSICAL_MEMORY_POLICY}
MEMORY_NAME = re.compile(r"^nv_ram_(rws|rwsp|rwst|rwsthp)_(\d+)x(\d+)$")
COMMON_PORTS = {
    "clk": ("input", ""),
    "ra": ("input", None),
    "re": ("input", ""),
    "dout": ("output", None),
    "wa": ("input", None),
    "we": ("input", ""),
    "di": ("input", None),
    "pwrbus_ram_pd": ("input", "[31:0]"),
}


def _expected_ports(
    kind: str, address_width: int, data_width: int
) -> Dict[str, Tuple[str, str]]:
    result = {
        **COMMON_PORTS,
        "ra": ("input", f"[{address_width - 1}:0]"),
        "wa": ("input", f"[{address_width - 1}:0]"),
        "dout": ("output", f"[{data_width - 1}:0]"),
        "di": ("input", f"[{data_width - 1}:0]"),
    }
    if kind in {"rwsp", "rwsthp"}:
        result["ore"] = ("input", "")
    if kind == "rwsthp":
        result["byp_sel"] = ("input", "")
        result["dbyp"] = ("input", f"[{data_width - 1}:0]")
    return result


def _emit_synthesizable_model(
    blocks: List[str],
    name: str,
    parameter: str,
    ports: List[Tuple[str, str, str]],
    macro_kind: str,
) -> None:
    match = MEMORY_NAME.fullmatch(name)
    if match is None:
        raise ValueError(f"{name}: unsupported NVDLA SRAM wrapper name")
    kind = match.group(1)
    depth = int(match.group(2))
    data_width = int(match.group(3))
    address_width = max(1, (depth - 1).bit_length())
    actual_ports = {
        port_name: (direction, width)
        for direction, width, port_name in ports
    }
    expected_widths = _expected_ports(kind, address_width, data_width)
    if set(actual_ports) != set(expected_widths):
        raise ValueError(f"{name}: unsupported synthesizable-model port set")
    for port_name, expected in expected_widths.items():
        if actual_ports[port_name] != expected:
            raise ValueError(
                f"{name}: {port_name} is {actual_ports[port_name]}, "
                f"expected {expected}"
            )

    if macro_kind not in {"RAMDP", "RAMPDP"}:
        raise ValueError(f"{name}: unsupported SRAM macro class {macro_kind!r}")
    blocks.append(f"module {name} #({parameter}) (")
    for index, (direction, width, port_name) in enumerate(ports):
        comma = "," if index + 1 < len(ports) else ""
        width_text = f" {width}" if width else ""
        blocks.append(f"  {direction}{width_text} {port_name}{comma}")
    # The pinned wrapper implements its extra bit as a flop array, with a
    # registered read address rather than a read-data register. Writes to the
    # retained address remain visible even while re is low.
    has_flop_bit = name == "nv_ram_rwsp_20x289"
    ram_width = data_width - 1 if has_flop_bit else data_width
    data_slice = f"[{ram_width - 1}:0]" if has_flop_bit else ""
    blocks.extend(
        [
            " );",
            '  (* ram_style = "block", syn_ramstyle = "block_ram" *)',
            f"  reg [{ram_width - 1}:0] mem [0:{depth - 1}];",
            f"  reg [{ram_width - 1}:0] read_data;",
        ]
    )
    if kind in {"rwsp", "rwsthp"}:
        blocks.append(f"  reg [{data_width - 1}:0] output_data;")
    if has_flop_bit:
        blocks.extend([
            '  (* ram_style = "registers" *) reg extra_bit [0:19];',
            "  reg [4:0] extra_read_address;",
            "  always @(posedge clk) begin",
            "    if (we) extra_bit[wa] <= di[288];",
            "    if (re) extra_read_address <= ra;",
            "  end",
        ])
    if macro_kind == "RAMPDP":
        # Keep the memory read register directly on mem[ra]. Register the
        # forwarding decision separately so FPGA tools can absorb the read
        # register into BRAM rather than infer an unsupported asynchronous port.
        blocks.extend([
            "  reg forwarded_read;",
            f"  reg [{ram_width - 1}:0] forwarded_data;",
            f"  wire [{ram_width - 1}:0] read_value = forwarded_read ? forwarded_data : read_data;",
        ])
    else:
        blocks.append(f"  wire [{ram_width - 1}:0] read_value = read_data;")
    # Match the pinned macros' SYNTHESIS behavior. RAMDP clamps in
    # retention; RAMPDP retains its output. Sleep bit 7 clamps both classes.
    blocked = "pwrbus_ram_pd[8] | pwrbus_ram_pd[7]" if macro_kind == "RAMDP" else "pwrbus_ram_pd[8]"
    clamp = "ram_access_blocked" if macro_kind == "RAMDP" else "pwrbus_ram_pd[7]"
    blocks.extend([
        f"  wire ram_access_blocked = {blocked};",
        f"  wire [{ram_width - 1}:0] ram_output = {clamp} ? {ram_width}'b0 : "
        + (f"((|pwrbus_ram_pd[6:0]) ? {ram_width}'bx : read_value);"
           if macro_kind == "RAMDP" else "read_value;"),
    ])
    if kind in {"rwst", "rwsthp"}:
        blocks.extend([
            "  reg write_through_valid;",
            f"  reg [{data_width - 1}:0] write_through_data;",
            "  wire write_through = re && we && (ra == wa);",
            f"  wire [{data_width - 1}:0] functional_read = write_through_valid ? write_through_data : ram_output;",
            "  always @(posedge clk) begin",
            "    if (re) write_through_valid <= write_through;",
            "    if (write_through) write_through_data <= di;",
            "  end",
        ])
    if macro_kind == "RAMPDP":
        # The original pseudo-dual-port macro captures write requests on the
        # rising edge and writes on the falling edge. Its gated core clock
        # preserves the last request while both enables are low, including
        # across retention. Forward that pending write into the first resumed
        # read rather than dropping a request captured during retention.
        blocks.extend([
            "  reg pending_write;",
            f"  reg [{address_width - 1}:0] pending_address;",
            f"  reg [{ram_width - 1}:0] pending_data;",
            "  always @(posedge clk) begin",
            "    if (re || we) pending_write <= we;",
            "    if (we) begin",
            "      pending_address <= wa;",
            f"      pending_data <= di{data_slice};",
            "    end",
            "  end",
            "  always @(negedge clk) begin",
            "    if (pending_write && !ram_access_blocked)",
            "      mem[pending_address] <= pending_data;",
            "  end",
            "  always @(posedge clk) begin",
            "    if (re && !ram_access_blocked) begin",
            "      read_data <= mem[ra];",
            "      forwarded_read <= pending_write && pending_address == ra;",
            "      forwarded_data <= pending_data;",
            "    end",
        ])
    else:
        blocks.extend([
            "  always @(posedge clk) begin",
            "    if (we && !ram_access_blocked)",
            f"      mem[wa] <= di{data_slice};",
            "    if (re && !ram_access_blocked)",
            "      read_data <= mem[ra];",
        ])
    if kind == "rwsp":
        output_value = "{extra_bit[extra_read_address], ram_output}" if has_flop_bit else "ram_output"
        blocks.extend(["    if (ore)", f"      output_data <= {output_value};"])
    elif kind == "rwsthp":
        blocks.extend(["    if (ore)", "      output_data <= byp_sel ? dbyp : functional_read;"])
    blocks.append("  end")
    output_value = "output_data" if kind in {"rwsp", "rwsthp"} else (
        "functional_read" if kind == "rwst" else "ram_output")
    blocks.append(f"  assign dout = {output_value};")
    blocks.extend(["endmodule", ""])


def generate(
    source_dir: Path,
    output: Path,
    memory_policy: str = BLACKBOX_MEMORY_POLICY,
) -> Tuple[int, int]:
    if memory_policy not in MEMORY_POLICIES:
        raise ValueError(f"unsupported NVDLA memory policy {memory_policy!r}")
    wrappers = sorted(
        path
        for path in source_dir.glob("nv_ram_*.v")
        if not path.name.endswith("_logic.v")
    )
    if not wrappers:
        raise ValueError(f"{source_dir}: no NVDLA SRAM wrappers found")
    blocks = [
        "// Generated from pinned NVDLA SRAM wrapper interfaces.",
        f"// Memory policy: {memory_policy}.",
        "// Physical models use inferred synchronous block RAMs.",
        "",
    ]
    modeled = 0
    names = set()
    for path in wrappers:
        name, parameter, ports = _parse_wrapper(path)
        if name in names:
            raise ValueError(f"{path}: duplicate wrapper module {name}")
        names.add(name)
        if memory_policy == PHYSICAL_MEMORY_POLICY:
            if MEMORY_NAME.fullmatch(name) is None:
                raise ValueError(f"{name}: unsupported NVDLA SRAM wrapper name")
            logic_path = path.with_name(path.stem + "_logic.v")
            if not logic_path.is_file():
                raise ValueError(f"{name}: physical model requires original wrapper logic")
            macro_kinds = set(re.findall(
                r"\b(RAMDP|RAMPDP)_[A-Z0-9_]+\s+ram_Inst_[A-Za-z0-9_]+\s*\(",
                logic_path.read_text(encoding="utf-8"),
            ))
            if len(macro_kinds) != 1:
                raise ValueError(f"{name}: unsupported or missing SRAM macro provenance")
            _emit_synthesizable_model(blocks, name, parameter, ports, macro_kinds.pop())
            modeled += 1
        else:
            blocks.append('(* black_box = "yes" *)')
            blocks.append(f"module {name} #({parameter}) (")
            for index, (direction, width, port_name) in enumerate(ports):
                comma = "," if index + 1 < len(ports) else ""
                width_text = f" {width}" if width else ""
                blocks.append(f"  {direction}{width_text} {port_name}{comma}")
            blocks.extend([" );", "endmodule", ""])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(blocks), encoding="utf-8")
    return len(wrappers), modeled


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate explicit NVDLA ASIC-SRAM abstraction models."
    )
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--memory-policy",
        choices=sorted(MEMORY_POLICIES),
        default=BLACKBOX_MEMORY_POLICY,
    )
    arguments = parser.parse_args()
    count, modeled = generate(
        arguments.source_dir.resolve(),
        arguments.output.resolve(),
        arguments.memory_policy,
    )
    print(
        f"generated_nvdla_ram_wrappers={count} "
        f"modeled_memories={modeled} output={arguments.output.resolve()}"
    )


if __name__ == "__main__":
    main()
