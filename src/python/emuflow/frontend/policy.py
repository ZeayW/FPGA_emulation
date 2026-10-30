"""Finite Yosys option grammar shared by bounded configuration consumers."""
import re

VALID_XILINX_FAMILIES = {"xcup", "xcu", "xc7"}
VALID_SYNTHESIS_POLICIES = {"native", "logic-only"}
DEFAULT_XILINX_MAPPING_STRATEGY = "hierarchical-abc9-v1"
VALID_XILINX_MAPPING_STRATEGIES = {
    DEFAULT_XILINX_MAPPING_STRATEGY, "hierarchical-classic-v1",
    "flatten-classic-v1", "flatten-abc9-v1",
}
YOSYS_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
YOSYS_DEFINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:=[^\s;]+)?$")
YOSYS_INCLUDE_DIR = re.compile(r"^[A-Za-z0-9_./:+-]+$")
