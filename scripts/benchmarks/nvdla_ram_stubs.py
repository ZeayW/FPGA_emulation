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
PHYSICAL_MEMORY_POLICY = "physically-implementable-shared-memory-model-v1"
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

    blocks.append(f"module {name} #({parameter}) (")
    for index, (direction, width, port_name) in enumerate(ports):
        comma = "," if index + 1 < len(ports) else ""
        width_text = f" {width}" if width else ""
        blocks.append(f"  {direction}{width_text} {port_name}{comma}")
    blocks.extend(
        [
            " );",
            '  (* ram_style = "block", syn_ramstyle = "block_ram" *)',
            f"  reg [{data_width - 1}:0] mem [0:{depth - 1}];",
            f"  reg [{data_width - 1}:0] read_data;",
        ]
    )
    if kind in {"rwsp", "rwsthp"}:
        blocks.append(f"  reg [{data_width - 1}:0] output_data;")
    blocks.extend(
        [
            "  always @(posedge clk) begin",
            "    if (we)",
            "      mem[wa] <= di;",
        ]
    )
    if kind in {"rwst", "rwsthp"}:
        blocks.extend(
            [
                "    if (re)",
                "      read_data <= (we && (wa == ra)) ? di : mem[ra];",
            ]
        )
    else:
        blocks.extend(["    if (re)", "      read_data <= mem[ra];"])
    if kind == "rwsp":
        blocks.extend(["    if (ore)", "      output_data <= read_data;"])
    elif kind == "rwsthp":
        blocks.extend(
            [
                "    if (ore)",
                "      output_data <= byp_sel ? dbyp : read_data;",
            ]
        )
    blocks.append("  end")
    blocks.append(
        "  assign dout = output_data;"
        if kind in {"rwsp", "rwsthp"}
        else "  assign dout = read_data;"
    )
    blocks.extend(
        [
            "  wire _unused_pwrbus = ^pwrbus_ram_pd;",
            "endmodule",
            "",
        ]
    )


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
            _emit_synthesizable_model(blocks, name, parameter, ports)
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
