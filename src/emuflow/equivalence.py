from __future__ import annotations

import hashlib
import heapq
import json
from collections import defaultdict
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from .errors import ValidationError
from .ir import EmuIR


def _bit(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value & 1
    text = str(value).strip().lower()
    if text in {"1", "1'b1", "true"}:
        return 1
    if text in {"0", "1'b0", "false", "x", "z"}:
        return 0
    return int(text, 2) & 1


def _stimulus(seed: int, cycle: int, port: str, bit: int) -> int:
    lower = port.lower()
    if lower in {"clk", "clock"}:
        return 0
    if lower.endswith(("resetn", "reset_n", "rstn", "rst_n")):
        return int(cycle >= 3)
    if lower in {"reset", "rst", "areset"}:
        return int(cycle < 3)
    digest = hashlib.sha256(
        f"{seed}:{cycle}:{port}:{bit}".encode("utf-8")
    ).digest()
    return digest[0] & 1


def _reset_deasserted_value(port: str) -> Optional[int]:
    lower = port.lower()
    if lower.endswith(("resetn", "reset_n", "rstn", "rst_n")):
        return 1
    if lower in {"reset", "rst", "areset"}:
        return 0
    return None


def _is_lut_type(cell_type: str) -> bool:
    return cell_type.startswith("LUT") or cell_type in {"$lut", "$_LUT_"}


def _is_ff_type(cell_type: str) -> bool:
    return cell_type in {"FDCE", "FDPE", "FDRE", "FDSE"} or (
        cell_type.startswith("$_DFF_")
    )


def _is_multiply_type(cell_type: str) -> bool:
    return cell_type in {"VTR_MULTIPLY", "DSP48E2"}


def _is_ram_type(cell_type: str) -> bool:
    return cell_type in {
        "VTR_SP_RAM",
        "VTR_DP_RAM",
        "RAMB18E2",
        "RAMB36E2",
    }


def _is_muxf_type(cell_type: str) -> bool:
    return cell_type in {"MUXF7", "MUXF8", "MUXF9"}


def _is_carry_type(cell_type: str) -> bool:
    return cell_type == "CARRY8"


def _is_combinational_type(cell_type: str) -> bool:
    return (
        _is_lut_type(cell_type)
        or _is_multiply_type(cell_type)
        or _is_muxf_type(cell_type)
        or _is_carry_type(cell_type)
    )


def _signed(value: int, width: int) -> int:
    value &= (1 << width) - 1
    return value - (1 << width) if value & (1 << (width - 1)) else value


def _parameter_text(instance: Mapping[str, Any], name: str) -> str:
    value = instance.get("parameters", {}).get(name)
    if value is None:
        raise ValidationError(
            f"instance {instance['id']!r} lacks parameter {name}"
        )
    return str(value).strip()


def _parameter_bits(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    text = str(value).strip().lower().replace("_", "")
    if not text:
        return 0
    if set(text) <= {"0", "1", "x", "z"}:
        return int(text.replace("x", "0").replace("z", "0"), 2)
    return int(text, 0)


def _interleaved_word(data: int, parity: int, lanes: int) -> int:
    value = 0
    for lane in range(lanes):
        value |= ((data >> (8 * lane)) & 0xFF) << (9 * lane)
        value |= ((parity >> lane) & 1) << (9 * lane + 8)
    return value


def _split_interleaved_word(value: int, lanes: int) -> Tuple[int, int]:
    data = 0
    parity = 0
    for lane in range(lanes):
        data |= ((value >> (9 * lane)) & 0xFF) << (8 * lane)
        parity |= ((value >> (9 * lane + 8)) & 1) << lane
    return data, parity


def _parameter_int(instance: Mapping[str, Any], name: str) -> int:
    raw = instance.get("parameters", {}).get(name)
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int):
        return raw
    text = str(raw).strip()
    if not text:
        raise ValidationError(
            f"instance {instance['id']!r} lacks integer parameter {name}"
        )
    if text.lower().startswith(("0x", "0b", "0o")):
        return int(text, 0)
    if set(text) <= {"0", "1"}:
        return int(text, 2)
    return int(text, 10)


def _lut_definition(
    instance: Mapping[str, Any],
) -> Tuple[int, str, str, int]:
    cell_type = instance["type"]
    parameters = instance.get("parameters", {})
    if cell_type == "$lut":
        width_text = str(parameters.get("WIDTH", ""))
        truth_text = str(parameters.get("LUT", ""))
        if not width_text or not truth_text:
            raise ValidationError(
                f"generic LUT {instance['id']!r} lacks WIDTH/LUT parameters"
            )
        return int(width_text, 2), "A", "Y", int(truth_text, 2)
    if cell_type == "$_LUT_":
        width_text = str(parameters.get("WIDTH", ""))
        truth_text = str(parameters.get("LUT", parameters.get("INIT", "")))
        if not width_text or not truth_text:
            raise ValidationError(
                f"generic LUT {instance['id']!r} lacks WIDTH/LUT parameters"
            )
        return int(width_text, 2), "A", "Y", int(truth_text, 2)
    width_text = cell_type[3:]
    if not width_text.isdigit():
        raise ValidationError(f"unsupported LUT primitive {cell_type!r}")
    init = parameters.get("INIT")
    if init is None:
        raise ValidationError(f"LUT {instance['id']!r} lacks INIT parameter")
    return int(width_text), "I", "O", int(str(init), 2)


class _MappedModel:
    def __init__(self, ir: EmuIR):
        self.ir = ir
        self.instances = {
            instance["id"]: instance for instance in ir.value["instances"]
        }
        unsupported = sorted(
            {
                instance["type"]
                for instance in self.instances.values()
                if not (
                    _is_combinational_type(instance["type"])
                    or _is_ff_type(instance["type"])
                    or _is_ram_type(instance["type"])
                )
            }
        )
        if unsupported:
            raise ValidationError(
                "cycle equivalence primitive model does not support "
                f"{unsupported}"
            )
        self.input_net: Dict[Tuple[str, str, int], str] = {}
        self.output_net: Dict[Tuple[str, str, int], str] = {}
        self.top_input_net: Dict[Tuple[str, int], str] = {}
        self.top_output_net: Dict[Tuple[str, int], str] = {}
        for net in ir.value["nets"]:
            for endpoint in net["drivers"]:
                key = (endpoint["port"], endpoint["bit"])
                if endpoint["instance"] is None:
                    self.top_input_net[key] = net["id"]
                else:
                    self.output_net[
                        (
                            endpoint["instance"],
                            endpoint["port"],
                            endpoint["bit"],
                        )
                    ] = net["id"]
            for endpoint in net["sinks"]:
                key = (endpoint["port"], endpoint["bit"])
                if endpoint["instance"] is None:
                    self.top_output_net[key] = net["id"]
                else:
                    self.input_net[
                        (
                            endpoint["instance"],
                            endpoint["port"],
                            endpoint["bit"],
                        )
                    ] = net["id"]
        self.constants: Dict[Tuple[str, str, int], int] = {}
        for instance in self.instances.values():
            for item in instance.get("constant_connections", []):
                self.constants[
                    (instance["id"], item["port"], item["bit"])
                ] = _bit(item["value"])
        self.ff_ids = sorted(
            instance_id
            for instance_id, instance in self.instances.items()
            if _is_ff_type(instance["type"])
        )
        self.lut_ids = sorted(
            instance_id
            for instance_id, instance in self.instances.items()
            if _is_lut_type(instance["type"])
        )
        self.multiply_ids = sorted(
            instance_id
            for instance_id, instance in self.instances.items()
            if _is_multiply_type(instance["type"])
        )
        self.ram_ids = sorted(
            instance_id
            for instance_id, instance in self.instances.items()
            if _is_ram_type(instance["type"])
        )
        self.muxf_ids = sorted(
            instance_id
            for instance_id, instance in self.instances.items()
            if _is_muxf_type(instance["type"])
        )
        self.carry_ids = sorted(
            instance_id
            for instance_id, instance in self.instances.items()
            if _is_carry_type(instance["type"])
        )
        self.combinational_ids = sorted(
            self.lut_ids
            + self.multiply_ids
            + self.muxf_ids
            + self.carry_ids
        )
        self.combinational_id_set = frozenset(self.combinational_ids)
        output_nets_by_instance: Dict[str, List[str]] = defaultdict(list)
        for (owner, _port, _bit_index), net in self.output_net.items():
            output_nets_by_instance[owner].append(net)
        self.combinational_output_nets: Dict[str, Tuple[str, ...]] = {
            instance_id: tuple(sorted(output_nets_by_instance[instance_id]))
            for instance_id in self.combinational_ids
        }
        driver_by_net = {
            net: instance_id
            for instance_id, nets in self.combinational_output_nets.items()
            for net in nets
        }
        dependencies: Dict[str, Set[str]] = {
            instance_id: set() for instance_id in self.combinational_ids
        }
        dependents: Dict[str, Set[str]] = defaultdict(set)
        for (instance_id, _port, _bit_index), net in self.input_net.items():
            if instance_id not in self.combinational_id_set:
                continue
            driver = driver_by_net.get(net)
            if driver is None:
                continue
            dependencies[instance_id].add(driver)
            dependents[driver].add(instance_id)
        indegree = {
            instance_id: len(items)
            for instance_id, items in dependencies.items()
        }
        ready = [
            instance_id
            for instance_id in self.combinational_ids
            if indegree[instance_id] == 0
        ]
        heapq.heapify(ready)
        order = []
        while ready:
            instance_id = heapq.heappop(ready)
            order.append(instance_id)
            for dependent in sorted(dependents.get(instance_id, ())):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    heapq.heappush(ready, dependent)
        if len(order) != len(self.combinational_ids):
            unresolved = sorted(
                set(self.combinational_ids) - set(order)
            )
            raise ValidationError(
                "mapped primitive simulation found unresolved combinational "
                f"cells {unresolved[:8]}"
            )
        self.combinational_order = tuple(order)
        self.combinational_order_index = {
            instance_id: index
            for index, instance_id in enumerate(self.combinational_order)
        }

    def _evaluate_dsp48e2(
        self,
        values: Dict[str, int],
        instance_id: str,
        overrides: Optional[Mapping[Tuple[str, str], int]],
    ) -> None:
        instance = self.instances[instance_id]
        for parameter in (
            "ACASCREG",
            "ADREG",
            "ALUMODEREG",
            "AREG",
            "BCASCREG",
            "BREG",
            "CARRYINREG",
            "CARRYINSELREG",
            "CREG",
            "DREG",
            "INMODEREG",
            "MREG",
            "OPMODEREG",
            "PREG",
        ):
            if _parameter_int(instance, parameter) != 0:
                raise ValidationError(
                    f"DSP48E2 {instance_id!r} uses unsupported registered "
                    f"parameter {parameter}"
                )
        required_text = {
            "A_INPUT": "DIRECT",
            "B_INPUT": "DIRECT",
            "USE_MULT": "MULTIPLY",
            "USE_SIMD": "ONE48",
            "AMULTSEL": "A",
            "BMULTSEL": "B",
        }
        for parameter, expected in required_text.items():
            if _parameter_text(instance, parameter).upper() != expected:
                raise ValidationError(
                    f"DSP48E2 {instance_id!r} parameter {parameter} is not "
                    f"the Yosys combinational-multiply contract"
                )
        output_ports = {
            port
            for owner, port, _bit in self.output_net
            if owner == instance_id
        }
        if output_ports - {"P"}:
            raise ValidationError(
                f"DSP48E2 {instance_id!r} exposes unsupported outputs "
                f"{sorted(output_ports - {'P'})}"
            )
        controls = {
            "INMODE": (5, 0),
            "ALUMODE": (4, 0),
            "OPMODE": (9, 5),
            "CARRYINSEL": (3, 0),
            "CARRYIN": (1, 0),
        }
        for port, (width, expected) in controls.items():
            actual = self._bus(
                values, instance_id, port, width, overrides
            )
            if actual != expected:
                raise ValidationError(
                    f"DSP48E2 {instance_id!r} port {port} is outside the "
                    "Yosys combinational-multiply contract"
                )
        left = self._bus(values, instance_id, "A", 30, overrides)
        right = self._bus(values, instance_id, "B", 18, overrides)
        if left is None or right is None:
            raise ValidationError(
                f"DSP48E2 {instance_id!r} has unresolved multiplier inputs"
            )
        product = _signed(left, 27) * _signed(right, 18)
        self._drive_bus(
            values, instance_id, "P", product & ((1 << 48) - 1), 48
        )

    def _evaluate_muxf(
        self,
        values: Dict[str, int],
        instance_id: str,
        overrides: Optional[Mapping[Tuple[str, str], int]],
    ) -> None:
        selected = self._pin(
            values, instance_id, "S", overrides=overrides
        )
        low = self._pin(
            values, instance_id, "I0", overrides=overrides
        )
        high = self._pin(
            values, instance_id, "I1", overrides=overrides
        )
        if selected is None or low is None or high is None:
            raise ValidationError(
                f"{self.instances[instance_id]['type']} {instance_id!r} "
                "has unresolved inputs"
            )
        output = self.output_net.get((instance_id, "O", 0))
        if output is not None:
            values[output] = int(high if selected else low)

    def _evaluate_carry8(
        self,
        values: Dict[str, int],
        instance_id: str,
        overrides: Optional[Mapping[Tuple[str, str], int]],
    ) -> None:
        instance = self.instances[instance_id]
        carry_type = _parameter_text(instance, "CARRY_TYPE").upper()
        if carry_type not in {"SINGLE_CY8", "DUAL_CY4"}:
            raise ValidationError(
                f"CARRY8 {instance_id!r} has unsupported CARRY_TYPE "
                f"{carry_type!r}"
            )
        ci = self._pin(values, instance_id, "CI", overrides=overrides)
        ci_top = self._pin(
            values, instance_id, "CI_TOP", overrides=overrides
        )
        select = self._bus(values, instance_id, "S", 8, overrides)
        data = self._bus(values, instance_id, "DI", 8, overrides)
        if None in {ci, ci_top, select, data}:
            raise ValidationError(
                f"CARRY8 {instance_id!r} has unresolved inputs"
            )
        carry = int(ci)
        carry_outputs = 0
        sum_outputs = 0
        for bit in range(8):
            if bit == 4 and carry_type == "DUAL_CY4":
                carry = int(ci_top)
            sum_outputs |= (((select >> bit) & 1) ^ carry) << bit
            carry = carry if (select >> bit) & 1 else (data >> bit) & 1
            carry_outputs |= carry << bit
        self._drive_bus(values, instance_id, "CO", carry_outputs, 8)
        self._drive_bus(values, instance_id, "O", sum_outputs, 8)

    def _evaluate_combinational_instance(
        self,
        values: Dict[str, int],
        instance_id: str,
        overrides: Optional[Mapping[Tuple[str, str], int]] = None,
    ) -> None:
        instance = self.instances[instance_id]
        if instance["type"] == "DSP48E2":
            self._evaluate_dsp48e2(values, instance_id, overrides)
            return
        if instance["type"] == "VTR_MULTIPLY":
            a_width = _parameter_int(instance, "A_WIDTH")
            b_width = _parameter_int(instance, "B_WIDTH")
            output_width = _parameter_int(instance, "Y_WIDTH")
            left = self._bus(
                values, instance_id, "a", a_width, overrides
            )
            right = self._bus(
                values, instance_id, "b", b_width, overrides
            )
            if left is None or right is None:
                raise ValidationError(
                    "mapped primitive simulation found unresolved "
                    f"combinational cells {[instance_id]}"
                )
            self._drive_bus(
                values,
                instance_id,
                "out",
                (left * right) & ((1 << output_width) - 1),
                output_width,
            )
            return
        if _is_muxf_type(instance["type"]):
            self._evaluate_muxf(values, instance_id, overrides)
            return
        if _is_carry_type(instance["type"]):
            self._evaluate_carry8(values, instance_id, overrides)
            return
        if instance["type"] == "LUT6_2":
            inputs = [
                self._pin(
                    values,
                    instance_id,
                    f"I{index}",
                    overrides=overrides,
                )
                for index in range(6)
            ]
            if any(value is None for value in inputs):
                raise ValidationError(
                    "mapped primitive simulation found unresolved "
                    f"combinational cells {[instance_id]}"
                )
            truth = _parameter_bits(
                instance.get("parameters", {}).get("INIT")
            )
            address = sum(
                int(value) << offset
                for offset, value in enumerate(inputs)
            )
            o5_net = self.output_net.get((instance_id, "O5", 0))
            if o5_net is not None:
                values[o5_net] = (truth >> (address & 0x1F)) & 1
            o6_net = self.output_net.get((instance_id, "O6", 0))
            if o6_net is not None:
                values[o6_net] = (truth >> address) & 1
            return
        width, input_port, output_port, truth = _lut_definition(instance)
        inputs = [
            self._pin(
                values,
                instance_id,
                f"I{index}" if input_port == "I" else input_port,
                bit=0 if input_port == "I" else index,
                overrides=overrides,
            )
            for index in range(width)
        ]
        if any(value is None for value in inputs):
            raise ValidationError(
                "mapped primitive simulation found unresolved combinational "
                f"cells {[instance_id]}"
            )
        address = sum(
            int(value) << offset
            for offset, value in enumerate(inputs)
        )
        output_net = self.output_net.get((instance_id, output_port, 0))
        if output_net is not None:
            values[output_net] = (truth >> address) & 1

    def _xilinx_ram_shape(
        self, instance_id: str
    ) -> Dict[str, Any]:
        instance = self.instances[instance_id]
        cell_type = instance["type"]
        if cell_type == "RAMB18E2":
            half_width, address_width = 18, 14
            write_enable_widths = {"A": 2, "B": 4}
        elif cell_type == "RAMB36E2":
            half_width, address_width = 36, 15
            write_enable_widths = {"A": 4, "B": 8}
        else:
            raise ValidationError(
                f"instance {instance_id!r} is not a modeled Xilinx BRAM"
            )
        widths = {
            "read_a": _parameter_int(instance, "READ_WIDTH_A"),
            "read_b": _parameter_int(instance, "READ_WIDTH_B"),
            "write_a": _parameter_int(instance, "WRITE_WIDTH_A"),
            "write_b": _parameter_int(instance, "WRITE_WIDTH_B"),
        }
        allowed = {0, 1, 2, 4, 9, 18}
        if half_width == 36:
            allowed.add(36)
        allowed.add(2 * half_width)
        invalid = {
            name: width
            for name, width in widths.items()
            if width not in allowed
        }
        if invalid:
            raise ValidationError(
                f"{cell_type} {instance_id!r} has unsupported widths "
                f"{invalid}"
            )
        wide = 2 * half_width
        if any(width == wide for width in widths.values()) and not (
            widths["read_a"] == wide
            and widths["write_b"] == wide
            and widths["read_b"] == 0
            and widths["write_a"] == 0
        ):
            raise ValidationError(
                f"{cell_type} {instance_id!r} uses an unsupported wide-port "
                "configuration"
            )
        for parameter in ("DOA_REG", "DOB_REG"):
            if _parameter_int(instance, parameter) != 0:
                raise ValidationError(
                    f"{cell_type} {instance_id!r} uses unsupported output "
                    f"register {parameter}"
                )
        for parameter in ("WRITE_MODE_A", "WRITE_MODE_B"):
            if _parameter_text(instance, parameter).upper() not in {
                "READ_FIRST",
                "WRITE_FIRST",
                "NO_CHANGE",
            }:
                raise ValidationError(
                    f"{cell_type} {instance_id!r} has unsupported "
                    f"{parameter}"
                )
        output_ports = {
            port
            for owner, port, _bit in self.output_net
            if owner == instance_id
        }
        supported_outputs = {
            "DOUTADOUT",
            "DOUTBDOUT",
            "DOUTPADOUTP",
            "DOUTPBDOUTP",
        }
        if output_ports - supported_outputs:
            raise ValidationError(
                f"{cell_type} {instance_id!r} exposes unsupported outputs "
                f"{sorted(output_ports - supported_outputs)}"
            )
        for name, value in instance.get("parameters", {}).items():
            if (
                (name.startswith("INIT_") and len(name) == 7)
                or (name.startswith("INITP_") and len(name) == 8)
            ) and _parameter_bits(value) != 0:
                raise ValidationError(
                    f"{cell_type} {instance_id!r} has initialized contents; "
                    "the cycle checker only accepts zero/unknown Yosys BRAM "
                    "initialization"
                )
        return {
            "cell_type": cell_type,
            "half_width": half_width,
            "address_width": address_width,
            "write_enable_widths": write_enable_widths,
            "widths": widths,
        }

    def _xilinx_ram_half_input(
        self,
        values: Mapping[str, int],
        instance_id: str,
        port: str,
        shape: Mapping[str, Any],
        overrides: Optional[Mapping[Tuple[str, str], int]],
    ) -> int:
        half_width = int(shape["half_width"])
        lanes = half_width // 9
        suffix = "A" if port == "A" else "B"
        data_port = "DINADIN" if port == "A" else "DINBDIN"
        parity_port = "DINPADINP" if port == "A" else "DINPBDINP"
        data = self._bus(
            values,
            instance_id,
            data_port,
            8 * lanes,
            overrides,
        )
        parity = self._bus(
            values,
            instance_id,
            parity_port,
            lanes,
            overrides,
        )
        if data is None or parity is None:
            raise ValidationError(
                f"{shape['cell_type']} {instance_id!r} port {suffix} has "
                "unresolved write data"
            )
        return _interleaved_word(data, parity, lanes)

    def _drive_xilinx_ram_outputs(
        self,
        values: Dict[str, int],
        instance_id: str,
        ram_state: Mapping[str, Any],
        shape: Mapping[str, Any],
    ) -> None:
        lanes = int(shape["half_width"]) // 9
        for port, data_port, parity_port in (
            ("a", "DOUTADOUT", "DOUTPADOUTP"),
            ("b", "DOUTBDOUT", "DOUTPBDOUTP"),
        ):
            data, parity = _split_interleaved_word(
                int(ram_state[f"out_{port}"]), lanes
            )
            self._drive_bus(
                values, instance_id, data_port, data, 8 * lanes
            )
            self._drive_bus(
                values, instance_id, parity_port, parity, lanes
            )

    @staticmethod
    def _xilinx_ram_masked_write(
        old_word: int,
        data: int,
        width: int,
        enables: int,
    ) -> Tuple[int, int]:
        lanes = (width + 8) // 9
        mask = 0
        for lane in range(lanes):
            if (enables >> lane) & 1:
                lane_width = min(9, width - 9 * lane)
                mask |= ((1 << lane_width) - 1) << (9 * lane)
        word_mask = (1 << width) - 1
        mask &= word_mask
        return ((old_word & ~mask) | (data & mask)) & word_mask, mask

    def _xilinx_ram_next_state(
        self,
        values: Mapping[str, int],
        instance_id: str,
        ram_state: Mapping[str, Any],
        overrides: Optional[Mapping[Tuple[str, str], int]],
    ) -> Dict[str, Any]:
        instance = self.instances[instance_id]
        shape = self._xilinx_ram_shape(instance_id)
        half_width = int(shape["half_width"])
        widths = shape["widths"]
        address_width = int(shape["address_width"])
        contents = dict(ram_state["contents"])
        out_a = int(ram_state["out_a"])
        out_b = int(ram_state["out_b"])
        sleep = self._pin(
            values, instance_id, "SLEEP", overrides=overrides
        )
        if sleep:
            return {
                "contents": contents,
                "out_a": out_a,
                "out_b": out_b,
            }
        addresses = {
            "A": self._bus(
                values,
                instance_id,
                "ADDRARDADDR",
                address_width,
                overrides,
            ),
            "B": self._bus(
                values,
                instance_id,
                "ADDRBWRADDR",
                address_width,
                overrides,
            ),
        }
        if any(value is None for value in addresses.values()):
            raise ValidationError(
                f"{shape['cell_type']} {instance_id!r} has unresolved address"
            )
        addresses = {key: int(value) for key, value in addresses.items()}
        halves = {
            port: self._xilinx_ram_half_input(
                values, instance_id, port, shape, overrides
            )
            for port in ("A", "B")
        }
        enables = {
            "A": self._bus(
                values,
                instance_id,
                "WEA",
                shape["write_enable_widths"]["A"],
                overrides,
            ),
            "B": self._bus(
                values,
                instance_id,
                "WEBWE",
                shape["write_enable_widths"]["B"],
                overrides,
            ),
        }
        writes = []
        if widths["write_a"]:
            writes.append((
                "A",
                addresses["A"],
                widths["write_a"],
                halves["A"],
                int(enables["A"] or 0),
            ))
        if widths["write_b"]:
            write_data = halves["B"]
            if widths["write_b"] > half_width:
                write_data = halves["A"] | (halves["B"] << half_width)
            writes.append((
                "B",
                addresses["B"],
                widths["write_b"],
                write_data,
                int(enables["B"] or 0),
            ))
        updated_contents = dict(contents)
        write_masks: Dict[Tuple[str, int], int] = {}
        for port, address, width, data, write_enable in writes:
            old_word = int(updated_contents.get(address, 0))
            new_word, mask = self._xilinx_ram_masked_write(
                old_word, data, width, write_enable
            )
            if mask:
                for (other_port, other_address), other_mask in write_masks.items():
                    if (
                        other_address == address
                        and other_mask & mask
                        and other_port != port
                    ):
                        raise ValidationError(
                            f"{shape['cell_type']} {instance_id!r} has an "
                            "ambiguous simultaneous dual-port write"
                        )
                updated_contents[address] = new_word
                write_masks[(port, address)] = mask

        def read_value(port: str, width: int, current: int) -> int:
            if not width:
                return current
            enable_port = "ENARDEN" if port == "A" else "ENBWREN"
            reset_port = "RSTRAMARSTRAM" if port == "A" else "RSTRAMB"
            enabled = self._pin(
                values, instance_id, enable_port, overrides=overrides
            )
            reset = self._pin(
                values, instance_id, reset_port, overrides=overrides
            )
            reset ^= _bit(
                instance.get("parameters", {}).get(
                    "IS_RSTRAMARSTRAM_INVERTED"
                    if port == "A"
                    else "IS_RSTRAMB_INVERTED",
                    0,
                )
            )
            if reset:
                return _parameter_bits(
                    instance.get("parameters", {}).get(
                        "SRVAL_A" if port == "A" else "SRVAL_B", 0
                    )
                ) & ((1 << min(width, half_width)) - 1)
            if not enabled:
                return current
            address = addresses[port]
            old_word = int(contents.get(address, 0))
            matching_write = any(
                write_address == address and write_mask
                for (_write_port, write_address), write_mask in write_masks.items()
            )
            mode = _parameter_text(
                instance, "WRITE_MODE_A" if port == "A" else "WRITE_MODE_B"
            ).upper()
            if matching_write and mode == "NO_CHANGE":
                return current
            source = (
                updated_contents.get(address, 0)
                if matching_write and mode == "WRITE_FIRST"
                else old_word
            )
            return int(source) & ((1 << width) - 1)

        if widths["read_a"] > half_width:
            wide_value = read_value(
                "A",
                widths["read_a"],
                out_a | (out_b << half_width),
            )
            out_a = wide_value & ((1 << half_width) - 1)
            out_b = (wide_value >> half_width) & ((1 << half_width) - 1)
        else:
            out_a = read_value("A", widths["read_a"], out_a)
            out_b = read_value("B", widths["read_b"], out_b)
        return {
            "contents": updated_contents,
            "out_a": out_a,
            "out_b": out_b,
        }

    def initial_state(self) -> Dict[str, Any]:
        state: Dict[str, Any] = {
            instance_id: _bit(
                self.instances[instance_id]
                .get("parameters", {})
                .get("INIT", 0)
            )
            for instance_id in self.ff_ids
        }
        for instance_id in self.ram_ids:
            instance = self.instances[instance_id]
            if instance["type"] in {"RAMB18E2", "RAMB36E2"}:
                shape = self._xilinx_ram_shape(instance_id)
                half_width = int(shape["half_width"])
                state[instance_id] = {
                    "contents": {},
                    "out_a": _parameter_bits(
                        instance.get("parameters", {}).get("INIT_A", 0)
                    ) & ((1 << half_width) - 1),
                    "out_b": _parameter_bits(
                        instance.get("parameters", {}).get("INIT_B", 0)
                    ) & ((1 << half_width) - 1),
                }
                continue
            width = _parameter_int(instance, "DATA_WIDTH")
            state[instance_id] = {
                "contents": {},
                **(
                    {"out": [0] * width}
                    if instance["type"] == "VTR_SP_RAM"
                    else {"out1": [0] * width, "out2": [0] * width}
                ),
            }
        return state

    def _pin(
        self,
        values: Mapping[str, int],
        instance_id: str,
        port: str,
        default: int = 0,
        overrides: Optional[Mapping[Tuple[str, str], int]] = None,
        bit: int = 0,
    ) -> Optional[int]:
        net = self.input_net.get((instance_id, port, bit))
        if net is not None:
            if overrides is not None and (instance_id, net) in overrides:
                return overrides[(instance_id, net)]
            return values.get(net)
        return self.constants.get((instance_id, port, bit), default)

    def _bus(
        self,
        values: Mapping[str, int],
        instance_id: str,
        port: str,
        width: int,
        overrides: Optional[Mapping[Tuple[str, str], int]] = None,
    ) -> Optional[int]:
        bits = [
            self._pin(
                values,
                instance_id,
                port,
                overrides=overrides,
                bit=bit,
            )
            for bit in range(width)
        ]
        if any(value is None for value in bits):
            return None
        return sum(int(value) << bit for bit, value in enumerate(bits))

    def _drive_bus(
        self,
        values: Dict[str, int],
        instance_id: str,
        port: str,
        value: int,
        width: int,
    ) -> None:
        for bit in range(width):
            net = self.output_net.get((instance_id, port, bit))
            if net is not None:
                values[net] = (value >> bit) & 1

    def evaluate(
        self,
        state: Mapping[str, Any],
        cycle: int,
        seed: int,
        overrides: Optional[Mapping[Tuple[str, str], int]] = None,
        input_values: Optional[Mapping[Tuple[str, int], int]] = None,
    ) -> Tuple[Dict[str, int], Dict[str, Any], Dict[str, int]]:
        values: Dict[str, int] = {}
        for (port, bit), net in self.top_input_net.items():
            if input_values is not None and (port, bit) in input_values:
                values[net] = _bit(input_values[(port, bit)])
            else:
                values[net] = _stimulus(seed, cycle, port, bit)
        for instance_id in self.ff_ids:
            q_net = self.output_net.get((instance_id, "Q", 0))
            if q_net is not None:
                values[q_net] = state[instance_id]
        for instance_id in self.ram_ids:
            instance = self.instances[instance_id]
            ram_state = state[instance_id]
            if instance["type"] in {"RAMB18E2", "RAMB36E2"}:
                self._drive_xilinx_ram_outputs(
                    values, instance_id, ram_state, self._xilinx_ram_shape(instance_id)
                )
                continue
            width = _parameter_int(instance, "DATA_WIDTH")
            if instance["type"] == "VTR_SP_RAM":
                self._drive_bus(
                    values,
                    instance_id,
                    "out",
                    sum(
                        int(bit) << index
                        for index, bit in enumerate(ram_state["out"])
                    ),
                    width,
                )
            else:
                for port in ("out1", "out2"):
                    self._drive_bus(
                        values,
                        instance_id,
                        port,
                        sum(
                            int(bit) << index
                            for index, bit in enumerate(ram_state[port])
                        ),
                        width,
                    )

        for instance_id in self.combinational_order:
            self._evaluate_combinational_instance(
                values, instance_id, overrides
            )

        next_state, outputs = self.state_and_outputs_from_values(
            values,
            state,
            overrides,
        )
        return values, next_state, outputs

    def state_and_outputs_from_values(
        self,
        values: Mapping[str, int],
        state: Mapping[str, Any],
        overrides: Optional[Mapping[Tuple[str, str], int]] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, int]]:
        """Extract the synchronous boundary from settled net values.

        Static-exact event simulation already maintains a settled local value
        map as transport shadows arrive.  Reusing that map here avoids a
        second whole-design combinational evaluation at macro-cycle commit.
        """

        next_state: Dict[str, Any] = {}
        for instance_id in self.ff_ids:
            instance = self.instances[instance_id]
            current = state[instance_id]
            data = self._pin(
                values, instance_id, "D", current, overrides
            )
            if instance["type"].startswith("$_DFF_"):
                next_state[instance_id] = int(data)
                continue
            data = int(data) ^ _bit(
                instance.get("parameters", {}).get("IS_D_INVERTED", 0)
            )
            enable = self._pin(
                values, instance_id, "CE", 1, overrides
            )
            if instance["type"] in {"FDRE", "FDCE"}:
                control_port = (
                    "R" if instance["type"] == "FDRE" else "CLR"
                )
                inversion_parameter = (
                    "IS_R_INVERTED"
                    if instance["type"] == "FDRE"
                    else "IS_CLR_INVERTED"
                )
                control = self._pin(
                    values, instance_id, control_port, 0, overrides
                )
                control = int(control) ^ _bit(
                    instance.get("parameters", {}).get(
                        inversion_parameter, 0
                    )
                )
                next_state[instance_id] = (
                    0 if control else int(data) if enable else current
                )
            else:
                control_port = (
                    "S" if instance["type"] == "FDSE" else "PRE"
                )
                inversion_parameter = (
                    "IS_S_INVERTED"
                    if instance["type"] == "FDSE"
                    else "IS_PRE_INVERTED"
                )
                control = self._pin(
                    values, instance_id, control_port, 0, overrides
                )
                control = int(control) ^ _bit(
                    instance.get("parameters", {}).get(
                        inversion_parameter, 0
                    )
                )
                next_state[instance_id] = (
                    1 if control else int(data) if enable else current
                )
        for instance_id in self.ram_ids:
            instance = self.instances[instance_id]
            ram_state = state[instance_id]
            if instance["type"] in {"RAMB18E2", "RAMB36E2"}:
                next_state[instance_id] = self._xilinx_ram_next_state(
                    values, instance_id, ram_state, overrides
                )
                continue
            contents = dict(ram_state["contents"])
            address_width = _parameter_int(instance, "ADDR_WIDTH")
            data_width = _parameter_int(instance, "DATA_WIDTH")
            word_mask = (1 << data_width) - 1
            if instance["type"] == "VTR_SP_RAM":
                address = self._bus(
                    values,
                    instance_id,
                    "addr",
                    address_width,
                    overrides,
                )
                data = self._bus(
                    values,
                    instance_id,
                    "data",
                    data_width,
                    overrides,
                )
                write_enable = self._pin(
                    values, instance_id, "we", overrides=overrides
                )
                if address is None or data is None or write_enable is None:
                    raise ValidationError(
                        f"RAM {instance_id!r} has unresolved synchronous input"
                    )
                read_word = int(contents.get(address, 0))
                if write_enable:
                    contents[address] = data & word_mask
                next_state[instance_id] = {
                    "contents": contents,
                    "out": [
                        (read_word >> bit) & 1
                        for bit in range(data_width)
                    ],
                }
            else:
                addresses = []
                data_words = []
                enables = []
                for port in (1, 2):
                    address = self._bus(
                        values,
                        instance_id,
                        f"addr{port}",
                        address_width,
                        overrides,
                    )
                    data = self._bus(
                        values,
                        instance_id,
                        f"data{port}",
                        data_width,
                        overrides,
                    )
                    enable = self._pin(
                        values,
                        instance_id,
                        f"we{port}",
                        overrides=overrides,
                    )
                    if address is None or data is None or enable is None:
                        raise ValidationError(
                            f"RAM {instance_id!r} has unresolved port {port}"
                        )
                    addresses.append(address)
                    data_words.append(data)
                    enables.append(enable)
                read_words = [
                    int(ram_state["contents"].get(address, 0))
                    for address in addresses
                ]
                for address, data, enable in zip(
                    addresses, data_words, enables
                ):
                    if enable:
                        contents[address] = data & word_mask
                next_state[instance_id] = {
                    "contents": contents,
                    **{
                        f"out{port}": [
                            (read_words[port - 1] >> bit) & 1
                            for bit in range(data_width)
                        ]
                        for port in (1, 2)
                    },
                }
        outputs = {
            f"{port}[{bit}]": values[net]
            for (port, bit), net in sorted(self.top_output_net.items())
            if net in values
        }
        return next_state, outputs

    def state_bit_count(self) -> int:
        ram_output_bits = 0
        for instance_id in self.ram_ids:
            instance = self.instances[instance_id]
            if instance["type"] in {"RAMB18E2", "RAMB36E2"}:
                shape = self._xilinx_ram_shape(instance_id)
                widths = shape["widths"]
                ram_output_bits += max(
                    widths["read_a"],
                    widths["read_b"],
                    int(shape["half_width"]),
                )
            else:
                ram_output_bits += _parameter_int(
                    instance, "DATA_WIDTH"
                ) * (1 if instance["type"] == "VTR_SP_RAM" else 2)
        return len(self.ff_ids) + ram_output_bits

    def evaluate_lut_subset(
        self,
        instance_ids: Set[str],
        reference_values: Mapping[str, int],
        overrides: Mapping[Tuple[str, str], int],
    ) -> Dict[str, int]:
        """Evaluate one fanin-closed replica cone without resimulating the DUT."""

        unsupported = sorted(set(instance_ids) - set(self.lut_ids))
        if unsupported:
            raise ValidationError(
                "replica subset contains non-LUT instances "
                f"{unsupported[:8]}"
            )
        values: Dict[str, int] = {}
        pending = set(instance_ids)
        while pending:
            progressed = False
            for instance_id in sorted(pending):
                instance = self.instances[instance_id]
                if instance["type"] == "LUT6_2":
                    inputs = []
                    unresolved = False
                    for index in range(6):
                        net = self.input_net.get(
                            (instance_id, f"I{index}", 0)
                        )
                        if net is None:
                            value = self.constants.get(
                                (instance_id, f"I{index}", 0), 0
                            )
                        elif (instance_id, net) in overrides:
                            value = overrides[(instance_id, net)]
                        elif net in values:
                            value = values[net]
                        else:
                            value = reference_values.get(net)
                        if value is None:
                            unresolved = True
                            break
                        inputs.append(value)
                    if unresolved:
                        continue
                    truth = _parameter_bits(
                        instance.get("parameters", {}).get("INIT")
                    )
                    address = sum(
                        int(value) << offset
                        for offset, value in enumerate(inputs)
                    )
                    for output_port, output_address in (
                        ("O5", address & 0x1F),
                        ("O6", address),
                    ):
                        output_net = self.output_net.get(
                            (instance_id, output_port, 0)
                        )
                        if output_net is not None:
                            values[output_net] = (
                                truth >> output_address
                            ) & 1
                    pending.remove(instance_id)
                    progressed = True
                    continue
                width, input_port, output_port, truth = _lut_definition(
                    instance
                )
                inputs = []
                unresolved = False
                for index in range(width):
                    port = (
                        f"I{index}" if input_port == "I" else input_port
                    )
                    bit = 0 if input_port == "I" else index
                    net = self.input_net.get(
                        (instance_id, port, bit)
                    )
                    if net is None:
                        value = self.constants.get(
                            (instance_id, port, bit), 0
                        )
                    elif (instance_id, net) in overrides:
                        value = overrides[(instance_id, net)]
                    elif net in values:
                        value = values[net]
                    else:
                        value = reference_values.get(net)
                    if value is None:
                        unresolved = True
                        break
                    inputs.append(value)
                if unresolved:
                    continue
                address = sum(
                    int(value) << offset
                    for offset, value in enumerate(inputs)
                )
                output_net = self.output_net.get(
                    (instance_id, output_port, 0)
                )
                if output_net is not None:
                    values[output_net] = (truth >> address) & 1
                pending.remove(instance_id)
                progressed = True
            if not progressed:
                raise ValidationError(
                    "replica subset simulation found unresolved "
                    f"LUTs {sorted(pending)[:8]}"
                )
        return values


def _static_exact_equivalence_context(
    ir: EmuIR,
    assignment: Mapping[str, Any],
    schedule: Mapping[str, Any],
) -> Dict[str, Any]:
    from .combinational_cut import semantic_contract_sha256
    from .routing import static_exact_contract_from_assignment
    from .tdm import (
        is_sampled_virtual_wire_schedule,
        sampled_virtual_wire_timing_constraints,
    )

    contract = static_exact_contract_from_assignment(assignment)
    if contract is None:
        raise ValidationError(
            "static exact macro-cycle equivalence requires an assignment "
            "semantic contract"
        )
    if not is_sampled_virtual_wire_schedule(schedule):
        raise ValidationError(
            "static exact macro-cycle equivalence requires sampled virtual-"
            "wire transport semantics"
        )
    digest = semantic_contract_sha256(contract)
    if schedule.get("semantic_contract_sha256") != digest:
        raise ValidationError(
            "static exact schedule is not bound to the assignment contract"
        )
    assignment_map = assignment.get("instance_assignment")
    if not isinstance(assignment_map, dict):
        raise ValidationError("assignment.instance_assignment must be an object")
    instance_ids = {item["id"] for item in ir.value["instances"]}
    if set(assignment_map) != instance_ids:
        raise ValidationError(
            "static exact assignment does not cover every EmuIR instance"
        )
    routes = schedule.get("routes")
    entries = schedule.get("entries")
    if not isinstance(routes, list) or not isinstance(entries, list):
        raise ValidationError("static exact schedule routes/entries must be arrays")
    if not all(
        isinstance(item, dict)
        and isinstance(item.get("net"), str)
        and isinstance(item.get("id"), str)
        for item in routes
    ):
        raise ValidationError("static exact schedule route metadata is malformed")
    route_by_net = {item["net"]: item for item in routes}
    route_by_id = {item["id"]: item for item in routes}
    if len(route_by_net) != len(routes) or len(route_by_id) != len(routes):
        raise ValidationError("static exact schedule route identities are not unique")
    cut_nodes = contract.get("cut_nodes")
    segments = contract.get("logic_segments")
    captures = contract.get("capture_requirements")
    if not all(isinstance(value, list) for value in (cut_nodes, segments, captures)):
        raise ValidationError("static exact contract arrays are malformed")
    if not all(isinstance(item, dict) for item in cut_nodes + segments + captures):
        raise ValidationError("static exact contract records must be objects")
    node_by_net = {item.get("net"): item for item in cut_nodes}
    segment_by_id = {item.get("id"): item for item in segments}
    capture_by_id = {item.get("id"): item for item in captures}
    if (
        None in node_by_net
        or None in segment_by_id
        or None in capture_by_id
        or len(node_by_net) != len(cut_nodes)
        or len(segment_by_id) != len(segments)
        or len(capture_by_id) != len(captures)
        or set(node_by_net) != set(route_by_net)
    ):
        raise ValidationError(
            "static exact contract identities/route coverage are invalid"
        )
    capture_segment_by_id: Dict[str, Mapping[str, Any]] = {}
    for segment in segments:
        if segment.get("kind") != "rx_to_capture":
            continue
        capture_id = segment.get("capture_requirement")
        if (
            not isinstance(capture_id, str)
            or capture_id not in capture_by_id
            or capture_id in capture_segment_by_id
        ):
            raise ValidationError(
                "static exact capture segment coverage is invalid"
            )
        capture_segment_by_id[capture_id] = segment
    if set(capture_segment_by_id) != set(capture_by_id):
        raise ValidationError(
            "static exact capture segment coverage is incomplete"
        )
    frame_slots = schedule.get("metrics", {}).get("frame_slots")
    timing_constraints = schedule.get("timing_constraints")
    if (
        isinstance(frame_slots, bool)
        or not isinstance(frame_slots, int)
        or frame_slots <= 1
        or timing_constraints
        != sampled_virtual_wire_timing_constraints(frame_slots)
    ):
        raise ValidationError("static exact frame/commit contract is invalid")
    commit_slot = timing_constraints["commit_slot"]
    entry_ids = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValidationError(
                f"static exact schedule entry {index} is not an object"
            )
        entry_id = entry.get("id")
        demand = entry.get("demand")
        if (
            not isinstance(entry_id, str)
            or entry_id in entry_ids
            or demand not in route_by_id
            or entry.get("net") != route_by_id[demand]["net"]
        ):
            raise ValidationError(
                f"static exact schedule entry {index} identity is invalid"
            )
        entry_ids.add(entry_id)
        for field in ("slot", "ready_slot", "arrival_slot"):
            value = entry.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValidationError(
                    f"static exact schedule entry {entry_id!r}.{field} is invalid"
                )
        if entry["slot"] >= frame_slots or entry["arrival_slot"] >= commit_slot:
            raise ValidationError(
                f"static exact schedule entry {entry_id!r} misses commit"
            )
    net_by_id = {item["id"]: item for item in ir.value["nets"]}
    if set(route_by_net) - set(net_by_id):
        raise ValidationError("static exact routes reference unknown EmuIR nets")
    combinational_ids = {
        item["id"]
        for item in ir.value["instances"]
        if _is_combinational_type(item["type"])
    }
    override_pins_by_shadow: Dict[
        Tuple[str, str], List[Tuple[str, str]]
    ] = defaultdict(list)
    local_comb_dependents_by_net: Dict[str, List[str]] = defaultdict(list)
    route_sink_sets = {
        net_id: frozenset(route["sinks"])
        for net_id, route in route_by_net.items()
    }
    for net_id, net in net_by_id.items():
        drivers = [
            endpoint["instance"]
            for endpoint in net["drivers"]
            if endpoint["instance"] is not None
        ]
        driver = drivers[0] if len(drivers) == 1 else None
        driver_fpga = assignment_map.get(driver) if driver is not None else None
        route = route_by_net.get(net_id)
        for endpoint in net["sinks"]:
            instance_id = endpoint["instance"]
            if instance_id is None:
                continue
            sink_fpga = assignment_map[instance_id]
            if route is not None and sink_fpga != route["source"]:
                if sink_fpga not in route_sink_sets[net_id]:
                    raise ValidationError(
                        f"static exact cut {net_id!r} omits sink FPGA "
                        f"{sink_fpga!r}"
                    )
                override_pins_by_shadow[(route["id"], sink_fpga)].append(
                    (instance_id, net_id)
                )
                continue
            if (
                instance_id in combinational_ids
                and (driver_fpga is None or driver_fpga == sink_fpga)
            ):
                local_comb_dependents_by_net[net_id].append(instance_id)
    return {
        "contract": contract,
        "assignment_map": assignment_map,
        "route_by_net": route_by_net,
        "route_by_id": route_by_id,
        "node_by_net": node_by_net,
        "segment_by_id": segment_by_id,
        "capture_by_id": capture_by_id,
        "capture_segment_by_id": capture_segment_by_id,
        "net_by_id": net_by_id,
        "entries": entries,
        "frame_slots": frame_slots,
        "commit_slot": commit_slot,
        "timing_constraints": timing_constraints,
        "override_pins_by_shadow": {
            key: tuple(sorted(set(value)))
            for key, value in override_pins_by_shadow.items()
        },
        "local_comb_dependents_by_net": {
            key: tuple(sorted(set(value)))
            for key, value in local_comb_dependents_by_net.items()
        },
    }


def _static_exact_sink_overrides(
    context: Mapping[str, Any],
    shadow_values: Mapping[Tuple[str, str], int],
) -> Dict[Tuple[str, str], int]:
    overrides: Dict[Tuple[str, str], int] = {}
    for shadow_key, pins in context["override_pins_by_shadow"].items():
        # An unavailable current-frame shadow is deliberately forced to a
        # local reset value.  This prevents the monolithic evaluator from
        # creating a hidden cross-FPGA bypass; readiness checks below must
        # prove that no TX/capture consumes this placeholder.
        value = int(shadow_values.get(shadow_key, 0))
        for pin in pins:
            overrides[pin] = value
    return overrides


class _StaticExactIncrementalValues:
    """Maintain local combinational values as transport shadows arrive."""

    def __init__(
        self,
        model: _MappedModel,
        context: Mapping[str, Any],
        state: Mapping[str, Any],
        cycle: int,
        seed: int,
        shadow_values: Mapping[Tuple[str, str], int],
        input_values: Optional[Mapping[Tuple[str, int], int]],
        reference_values: Optional[Mapping[str, int]] = None,
    ) -> None:
        self.model = model
        self.context = context
        self.overrides: Dict[Tuple[str, str], int] = {}
        if reference_values is None:
            self.values, _, _ = model.evaluate(
                state,
                cycle,
                seed,
                input_values=input_values,
            )
            self.initial_full_evaluations = 1
        else:
            self.values = dict(reference_values)
            self.initial_full_evaluations = 0
        self.incremental_cell_evaluations = 0
        self.shadow_pin_updates = 0
        # Start from the monolithic reference snapshot and apply every local
        # cross-FPGA shadow override as one batch.  Each affected local cone is
        # then recomputed at most once in topological order, instead of doing a
        # second whole-design evaluation merely to initialize partition-local
        # values.
        self.apply_shadow_updates(
            [
                (shadow_key, int(shadow_values.get(shadow_key, 0)))
                for shadow_key in sorted(context["override_pins_by_shadow"])
            ]
        )

    def value(self, net_id: str) -> int:
        if net_id not in self.values:
            raise ValidationError(
                f"static exact TX source {net_id!r} is unresolved"
            )
        return int(self.values[net_id])

    def apply_shadow_updates(
        self,
        updates: List[Tuple[Tuple[str, str], int]],
    ) -> None:
        dirty = set()
        for shadow_key, value in updates:
            for pin in self.context["override_pins_by_shadow"].get(
                shadow_key, ()
            ):
                if self.overrides.get(pin) == int(value):
                    continue
                self.overrides[pin] = int(value)
                self.shadow_pin_updates += 1
                if pin[0] in self.model.combinational_id_set:
                    dirty.add(pin[0])
        pending = [
            (self.model.combinational_order_index[item], item)
            for item in dirty
        ]
        heapq.heapify(pending)
        queued = set(dirty)
        while pending:
            _index, instance_id = heapq.heappop(pending)
            queued.remove(instance_id)
            output_nets = self.model.combinational_output_nets[instance_id]
            before = tuple(self.values.get(net) for net in output_nets)
            self.model._evaluate_combinational_instance(
                self.values, instance_id, self.overrides
            )
            self.incremental_cell_evaluations += 1
            after = tuple(self.values.get(net) for net in output_nets)
            for net, old_value, new_value in zip(
                output_nets, before, after
            ):
                if old_value == new_value:
                    continue
                for dependent in self.context[
                    "local_comb_dependents_by_net"
                ].get(net, ()):
                    if dependent in queued:
                        continue
                    heapq.heappush(
                        pending,
                        (
                            self.model.combinational_order_index[dependent],
                            dependent,
                        ),
                    )
                    queued.add(dependent)


def _static_exact_source_ready_slot(
    node: Mapping[str, Any],
    segment_by_id: Mapping[str, Mapping[str, Any]],
    current_arrivals: Mapping[Tuple[str, str], int],
    timing_constraints: Mapping[str, Any],
) -> Tuple[int, List[Dict[str, Any]]]:
    evidence = []
    predecessor_coverage = []
    source_fpgas = node.get("source_fpgas")
    if not isinstance(source_fpgas, list) or len(source_fpgas) != 1:
        raise ValidationError(
            f"static exact cut {node.get('net')!r} has invalid source FPGA"
        )
    for segment_id in node.get("source_segment_ids", []):
        segment = segment_by_id.get(segment_id)
        if segment is None:
            raise ValidationError(
                f"static exact cut {node.get('net')!r} references unknown "
                f"segment {segment_id!r}"
            )
        from .tdm import sampled_logic_segment_budget_slots

        budget = sampled_logic_segment_budget_slots(
            segment, timing_constraints
        )
        if segment.get("kind") == "launch_to_tx":
            if segment.get("sink_cut_net") != node.get("net"):
                raise ValidationError(
                    f"static exact launch segment {segment_id!r} is misbound"
                )
            evidence.append(
                {
                    "segment": segment_id,
                    "kind": "launch_to_tx",
                    "ready_slot": budget,
                }
            )
            continue
        if segment.get("kind") != "rx_to_tx":
            raise ValidationError(
                f"static exact source segment {segment_id!r} has invalid kind"
            )
        predecessor = segment.get("source_cut_net")
        key = (predecessor, source_fpgas[0])
        if (
            segment.get("sink_cut_net") != node.get("net")
            or segment.get("fpga") != source_fpgas[0]
            or predecessor not in node.get("predecessor_cut_nets", [])
            or key not in current_arrivals
        ):
            raise ValidationError(
                f"static exact dependency {segment_id!r} is not ready in the "
                "current macro-cycle"
            )
        arrival = current_arrivals[key]
        predecessor_coverage.append(predecessor)
        evidence.append(
            {
                "segment": segment_id,
                "kind": "rx_to_tx",
                "predecessor_cut_net": predecessor,
                "arrival_slot": arrival,
                "ready_slot": arrival + budget,
            }
        )
    if not evidence:
        raise ValidationError(
            f"static exact cut {node.get('net')!r} has no readiness evidence"
        )
    if sorted(predecessor_coverage) != sorted(
        node.get("predecessor_cut_nets", [])
    ):
        raise ValidationError(
            f"static exact cut {node.get('net')!r} predecessor coverage is "
            "incomplete"
        )
    return max(item["ready_slot"] for item in evidence), evidence


def _simulate_static_exact_macro_step(
    model: _MappedModel,
    context: Mapping[str, Any],
    state: Mapping[str, Any],
    cycle: int,
    seed: int,
    shadow_values: Mapping[Tuple[str, str], int],
    input_values: Optional[Mapping[Tuple[str, int], int]] = None,
    *,
    full_replay_source_values: bool = False,
) -> Dict[str, Any]:
    reference_values, reference_next, reference_outputs = model.evaluate(
        state,
        cycle,
        seed,
        input_values=input_values,
    )
    shadows = dict(shadow_values)
    incremental_values = (
        None
        if full_replay_source_values
        else _StaticExactIncrementalValues(
            model,
            context,
            state,
            cycle,
            seed,
            shadows,
            input_values,
            reference_values,
        )
    )
    if incremental_values is None:
        # The legacy small-model oracle deliberately resolves TX values by
        # repeated full replay and must not retain a reference-net shortcut.
        del reference_values
    current_arrivals: Dict[Tuple[str, str], int] = {}
    current_shadow_generation: Set[Tuple[str, str]] = set()
    arrivals_by_slot: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    entries_by_slot: Dict[int, List[Mapping[str, Any]]] = defaultdict(list)
    for entry in context["entries"]:
        entries_by_slot[entry["slot"]].append(entry)
    tx_samples = []
    source_ready_checks = 0
    relay_ready_checks = 0
    uninitialized_shadow_reads = 0
    source_full_evaluations = 0
    for slot in range(context["frame_slots"]):
        source_value_cache: Dict[str, int] = {}
        for entry in sorted(
            entries_by_slot.get(slot, []),
            key=lambda item: (item["hop"], item["id"]),
        ):
            route = context["route_by_id"][entry["demand"]]
            if entry["from"] == route["source"]:
                node = context["node_by_net"][entry["net"]]
                ready_slot, readiness = _static_exact_source_ready_slot(
                    node,
                    context["segment_by_id"],
                    current_arrivals,
                    context["timing_constraints"],
                )
                source_ready_checks += 1
                if entry["ready_slot"] != ready_slot or slot < ready_slot:
                    raise ValidationError(
                        f"static exact TX {entry['id']!r} samples net "
                        f"{entry['net']!r} at slot {slot}, before source-ready "
                        f"slot {ready_slot}"
                    )
                if full_replay_source_values:
                    if entry["net"] not in source_value_cache:
                        overrides = _static_exact_sink_overrides(
                            context, shadows
                        )
                        local_values, _, _ = model.evaluate(
                            state,
                            cycle,
                            seed,
                            overrides=overrides,
                            input_values=input_values,
                        )
                        source_full_evaluations += 1
                        if entry["net"] not in local_values:
                            raise ValidationError(
                                f"static exact TX source {entry['net']!r} "
                                "is unresolved"
                            )
                        source_value_cache[entry["net"]] = local_values[
                            entry["net"]
                        ]
                    value = source_value_cache[entry["net"]]
                else:
                    assert incremental_values is not None
                    value = incremental_values.value(entry["net"])
                evidence = readiness
            else:
                key = (entry["demand"], entry["from"])
                logical_key = (entry["net"], entry["from"])
                if (
                    key not in current_shadow_generation
                    or logical_key not in current_arrivals
                ):
                    uninitialized_shadow_reads += 1
                    raise ValidationError(
                        f"static exact relay {entry['id']!r} consumes a stale "
                        "or unavailable shadow"
                    )
                ready_slot = current_arrivals[logical_key] + 1
                relay_ready_checks += 1
                if entry["ready_slot"] != ready_slot or slot < ready_slot:
                    raise ValidationError(
                        f"static exact relay {entry['id']!r} samples before "
                        f"ready slot {ready_slot}"
                    )
                value = shadows[key]
                evidence = [
                    {
                        "kind": "route_tree_relay",
                        "arrival_slot": current_arrivals[logical_key],
                        "ready_slot": ready_slot,
                    }
                ]
            arrivals_by_slot[entry["arrival_slot"]].append(
                {
                    "entry": entry,
                    "value": int(value),
                }
            )
            tx_samples.append(
                {
                    "entry": entry["id"],
                    "net": entry["net"],
                    "fpga": entry["from"],
                    "slot": slot,
                    "value": int(value),
                    "readiness": evidence,
                }
            )
        # TX samples the pre-edge shadow state. RX nonblocking updates at the
        # same labelled edge become visible only after all TX samples here.
        shadow_updates = []
        for event in sorted(
            arrivals_by_slot.pop(slot, []),
            key=lambda item: item["entry"]["id"],
        ):
            entry = event["entry"]
            shadow_key = (entry["demand"], entry["to"])
            if shadow_key in current_shadow_generation:
                raise ValidationError(
                    f"static exact demand {entry['demand']!r} has multiple "
                    f"current-frame arrivals at {entry['to']!r}"
                )
            shadows[shadow_key] = event["value"]
            shadow_updates.append((shadow_key, int(event["value"])))
            current_shadow_generation.add(shadow_key)
            current_arrivals[(entry["net"], entry["to"])] = slot
        if incremental_values is not None and shadow_updates:
            incremental_values.apply_shadow_updates(shadow_updates)
    if arrivals_by_slot:
        raise ValidationError("static exact schedule has arrivals after frame end")

    capture_checks = 0
    for capture_id, capture in sorted(context["capture_by_id"].items()):
        segment = context["capture_segment_by_id"][capture_id]
        key = (capture.get("cut_net"), capture.get("fpga"))
        if key not in current_arrivals:
            raise ValidationError(
                f"static exact capture {capture_id!r} consumes no current-"
                "frame arrival"
            )
        from .tdm import sampled_logic_segment_budget_slots

        ready_slot = current_arrivals[key] + sampled_logic_segment_budget_slots(
            segment, context["timing_constraints"]
        )
        if ready_slot > context["commit_slot"]:
            raise ValidationError(
                f"static exact capture {capture_id!r} is ready at "
                f"{ready_slot}, after commit {context['commit_slot']}"
            )
        capture_checks += 1

    final_overrides = _static_exact_sink_overrides(context, shadows)
    if incremental_values is None:
        _, partition_next, partition_outputs = model.evaluate(
            state,
            cycle,
            seed,
            overrides=final_overrides,
            input_values=input_values,
        )
        partition_full_evaluations = 1
        initialization_full_evaluations = 0
    else:
        partition_next, partition_outputs = (
            model.state_and_outputs_from_values(
                incremental_values.values,
                state,
                incremental_values.overrides,
            )
        )
        partition_full_evaluations = 0
        initialization_full_evaluations = (
            incremental_values.initial_full_evaluations
        )
    if partition_next != reference_next:
        mismatch = next(
            instance_id
            for instance_id in sorted(reference_next)
            if partition_next.get(instance_id) != reference_next[instance_id]
        )
        raise ValidationError(
            f"macro-cycle {cycle}: static exact partition state mismatch at "
            f"{mismatch!r}"
        )
    if partition_outputs != reference_outputs:
        raise ValidationError(
            f"macro-cycle {cycle}: static exact partition top-output mismatch"
        )
    return {
        "next_state": reference_next,
        "outputs": reference_outputs,
        "shadow_values": shadows,
        "tx_samples": tx_samples,
        "source_ready_checks": source_ready_checks,
        "relay_ready_checks": relay_ready_checks,
        "capture_checks": capture_checks,
        "uninitialized_shadow_reads": uninitialized_shadow_reads,
        "source_full_evaluations": source_full_evaluations,
        "reference_full_evaluations": 1,
        "initialization_full_evaluations": initialization_full_evaluations,
        "partition_full_evaluations": partition_full_evaluations,
        "incremental_cell_evaluations": (
            0
            if incremental_values is None
            else incremental_values.incremental_cell_evaluations
        ),
        "shadow_pin_updates": (
            0
            if incremental_values is None
            else incremental_values.shadow_pin_updates
        ),
    }


def simulate_static_exact_partition_equivalence(
    ir: EmuIR,
    assignment: Mapping[str, Any],
    schedule: Mapping[str, Any],
    cycles: int = 16,
    seed: int = 20260727,
) -> Dict[str, Any]:
    """Random-trace exact-cut equivalence with slot-accurate local TX values."""
    if isinstance(cycles, bool) or not isinstance(cycles, int) or cycles <= 0:
        raise ValidationError("equivalence cycles must be positive")
    model = _MappedModel(ir)
    context = _static_exact_equivalence_context(ir, assignment, schedule)
    state = model.initial_state()
    shadow_values: Dict[Tuple[str, str], int] = {}
    trace = hashlib.sha256()
    tx_samples = 0
    source_ready_checks = 0
    relay_ready_checks = 0
    capture_checks = 0
    uninitialized_shadow_reads = 0
    source_full_evaluations = 0
    reference_full_evaluations = 0
    initialization_full_evaluations = 0
    partition_full_evaluations = 0
    incremental_cell_evaluations = 0
    shadow_pin_updates = 0
    compared_outputs = 0
    compared_state_bits = 0
    # Start after the deterministic reset stimulus interval. Shadow validity
    # is still empty, so the first post-reset frame proves it consumes only
    # current-frame arrivals rather than reset/stale implementation state.
    for macro_cycle in range(cycles):
        stimulus_cycle = macro_cycle + 3
        result = _simulate_static_exact_macro_step(
            model,
            context,
            state,
            stimulus_cycle,
            seed,
            shadow_values,
        )
        state = result["next_state"]
        shadow_values = result["shadow_values"]
        tx_samples += len(result["tx_samples"])
        source_ready_checks += result["source_ready_checks"]
        relay_ready_checks += result["relay_ready_checks"]
        capture_checks += result["capture_checks"]
        uninitialized_shadow_reads += result["uninitialized_shadow_reads"]
        source_full_evaluations += result["source_full_evaluations"]
        reference_full_evaluations += result[
            "reference_full_evaluations"
        ]
        initialization_full_evaluations += result[
            "initialization_full_evaluations"
        ]
        partition_full_evaluations += result[
            "partition_full_evaluations"
        ]
        incremental_cell_evaluations += result[
            "incremental_cell_evaluations"
        ]
        shadow_pin_updates += result["shadow_pin_updates"]
        compared_outputs += len(result["outputs"])
        compared_state_bits += model.state_bit_count()
        trace.update(
            json.dumps(
                {
                    "cycle": macro_cycle,
                    "next_state": result["next_state"],
                    "outputs": result["outputs"],
                    "tx_samples": result["tx_samples"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    return {
        "status": "pass",
        "provider": "static-exact-event-driven-macro-cycle-model-v1",
        "evidence_type": "random-simulation",
        "qualification": "randomized-trace-validation-not-proof",
        "cycles": cycles,
        "seed": seed,
        "primitive_instances": len(model.instances),
        "flip_flops": len(model.ff_ids),
        "luts": len(model.lut_ids),
        "multipliers": len(model.multiply_ids),
        "memory_macros": len(model.ram_ids),
        "tx_samples": tx_samples,
        "source_ready_checks": source_ready_checks,
        "relay_ready_checks": relay_ready_checks,
        "capture_checks": capture_checks,
        "startup_uninitialized_shadow_reads": uninitialized_shadow_reads,
        "source_full_evaluations": source_full_evaluations,
        "reference_full_evaluations": reference_full_evaluations,
        "initialization_full_evaluations": (
            initialization_full_evaluations
        ),
        "partition_full_evaluations": partition_full_evaluations,
        "incremental_combinational_cell_evaluations": (
            incremental_cell_evaluations
        ),
        "shadow_pin_updates": shadow_pin_updates,
        "compared_state_bits": compared_state_bits,
        "compared_output_bits": compared_outputs,
        "mismatches": 0,
        "trace_sha256": trace.hexdigest(),
    }


def exhaustively_verify_static_exact_partition_equivalence(
    ir: EmuIR,
    assignment: Mapping[str, Any],
    schedule: Mapping[str, Any],
    *,
    max_variables: int = 12,
) -> Dict[str, Any]:
    """Exhaust every FF state and non-clock primary input for a small model."""
    if (
        isinstance(max_variables, bool)
        or not isinstance(max_variables, int)
        or max_variables <= 0
    ):
        raise ValidationError("exhaustive max_variables must be positive")
    model = _MappedModel(ir)
    if model.ram_ids:
        raise ValidationError(
            "exhaustive static exact proof does not support memory state"
        )
    context = _static_exact_equivalence_context(ir, assignment, schedule)
    reset_values = {
        key: value
        for key in model.top_input_net
        if (value := _reset_deasserted_value(key[0])) is not None
    }
    input_keys = sorted(
        key
        for key in model.top_input_net
        if key[0].lower() not in {"clk", "clock"}
        and key not in reset_values
    )
    variables = len(model.ff_ids) + len(input_keys)
    if variables > max_variables:
        raise ValidationError(
            "exhaustive static exact proof variable limit exceeded: "
            f"{variables} > {max_variables}"
        )
    cases = 1 << variables
    trace = hashlib.sha256()
    source_ready_checks = 0
    relay_ready_checks = 0
    capture_checks = 0
    tx_samples = 0
    full_replay_cross_checks = 0
    source_full_evaluations = 0
    reference_full_evaluations = 0
    initialization_full_evaluations = 0
    partition_full_evaluations = 0
    incremental_cell_evaluations = 0
    shadow_pin_updates = 0
    for vector in range(cases):
        bits = [(vector >> index) & 1 for index in range(variables)]
        state = {
            instance_id: bits[index]
            for index, instance_id in enumerate(model.ff_ids)
        }
        input_values = {
            key: bits[len(model.ff_ids) + index]
            for index, key in enumerate(input_keys)
        }
        input_values.update(reset_values)
        result = _simulate_static_exact_macro_step(
            model,
            context,
            state,
            0,
            0,
            {},
            input_values=input_values,
        )
        full_replay = _simulate_static_exact_macro_step(
            model,
            context,
            state,
            0,
            0,
            {},
            input_values=input_values,
            full_replay_source_values=True,
        )
        for field in (
            "next_state",
            "outputs",
            "shadow_values",
            "tx_samples",
            "source_ready_checks",
            "relay_ready_checks",
            "capture_checks",
        ):
            if result[field] != full_replay[field]:
                raise ValidationError(
                    "incremental static exact event model disagrees with "
                    f"full-replay oracle for vector {vector} field {field!r}"
                )
        full_replay_cross_checks += 1
        source_full_evaluations += result["source_full_evaluations"]
        reference_full_evaluations += result[
            "reference_full_evaluations"
        ]
        initialization_full_evaluations += result[
            "initialization_full_evaluations"
        ]
        partition_full_evaluations += result[
            "partition_full_evaluations"
        ]
        incremental_cell_evaluations += result[
            "incremental_cell_evaluations"
        ]
        shadow_pin_updates += result["shadow_pin_updates"]
        source_ready_checks += result["source_ready_checks"]
        relay_ready_checks += result["relay_ready_checks"]
        capture_checks += result["capture_checks"]
        tx_samples += len(result["tx_samples"])
        trace.update(
            json.dumps(
                {
                    "state": state,
                    "inputs": {
                        f"{port}[{bit}]": value
                        for (port, bit), value in input_values.items()
                    },
                    "next_state": result["next_state"],
                    "outputs": result["outputs"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    return {
        "status": "pass",
        "provider": "static-exact-exhaustive-macro-step-v1",
        "evidence_type": "exhaustive-small-model",
        "qualification": "complete-enumeration-proof-for-declared-model",
        "state_bits": len(model.ff_ids),
        "primary_input_bits": len(input_keys),
        "constrained_reset_bits": len(reset_values),
        "assumptions": [
            "one-commit macro-step semantics",
            "reset inputs constrained to their deasserted values",
            "transport shadows begin unavailable and must be produced in-frame",
        ],
        "variables": variables,
        "cases": cases,
        "source_ready_checks": source_ready_checks,
        "relay_ready_checks": relay_ready_checks,
        "capture_checks": capture_checks,
        "tx_samples": tx_samples,
        "full_replay_cross_checks": full_replay_cross_checks,
        "source_full_evaluations": source_full_evaluations,
        "reference_full_evaluations": reference_full_evaluations,
        "initialization_full_evaluations": (
            initialization_full_evaluations
        ),
        "partition_full_evaluations": partition_full_evaluations,
        "incremental_combinational_cell_evaluations": (
            incremental_cell_evaluations
        ),
        "shadow_pin_updates": shadow_pin_updates,
        "mismatches": 0,
        "trace_sha256": trace.hexdigest(),
    }


def simulate_partition_equivalence(
    ir: EmuIR,
    assignment: Mapping[str, Any],
    schedule: Mapping[str, Any],
    cycles: int = 16,
    seed: int = 20260727,
) -> Dict[str, Any]:
    if cycles <= 0:
        raise ValidationError("equivalence cycles must be positive")
    model = _MappedModel(ir)
    assignment_map = assignment["instance_assignment"]
    cut_class_by_net = {
        cut["net"]: cut["cut_class"] for cut in assignment["cut_nets"]
    }
    route_source = {}
    for cut in assignment["cut_nets"]:
        route_source[cut["net"]] = cut["source_fpgas"][0]

    route_by_net = {
        route["net"]: route for route in schedule.get("routes", [])
    }
    replica_records = assignment.get("replication", {}).get("replicas", [])
    output_nets_by_instance: Dict[str, Set[str]] = {
        instance_id: set() for instance_id in model.instances
    }
    sink_nets_by_instance: Dict[str, Set[str]] = {
        instance_id: set() for instance_id in model.instances
    }
    for net in ir.value["nets"]:
        for endpoint in net["drivers"]:
            if endpoint["instance"] is not None:
                output_nets_by_instance[endpoint["instance"]].add(net["id"])
        for endpoint in net["sinks"]:
            if endpoint["instance"] is not None:
                sink_nets_by_instance[endpoint["instance"]].add(net["id"])
    first_source_slot: Dict[str, int] = {}
    completion_by_round: Dict[int, int] = {}
    for entry in schedule.get("entries", []):
        if entry["from"] == route_source[entry["net"]]:
            first_source_slot[entry["net"]] = min(
                entry["slot"],
                first_source_slot.get(entry["net"], entry["slot"]),
            )
        transport_round = route_by_net[entry["net"]].get(
            "transport_round", 0
        )
        completion_by_round[transport_round] = max(
            entry["arrival_slot"],
            completion_by_round.get(
                transport_round, entry["arrival_slot"]
            ),
        )
    round_barrier_checks = 0
    for net_id, route in route_by_net.items():
        transport_round = route.get("transport_round", 0)
        prior_completions = [
            completion
            for round_index, completion in completion_by_round.items()
            if round_index < transport_round
        ]
        if not prior_completions:
            continue
        round_barrier_checks += 1
        required_slot = max(prior_completions) + 1
        source_slot = first_source_slot.get(net_id)
        if source_slot is None or source_slot < required_slot:
            raise ValidationError(
                f"cut net {net_id!r} in transport round "
                f"{transport_round} is sent at {source_slot!r}, before "
                f"round barrier slot {required_slot}"
            )

    state = model.initial_state()
    trace = hashlib.sha256()
    compared_outputs = 0
    compared_state_bits = 0
    compared_replica_outputs = 0
    for cycle in range(cycles):
        reference_values, reference_next, reference_outputs = model.evaluate(
            state, cycle, seed
        )
        shadow: Dict[Tuple[str, str], int] = {}
        for entry in sorted(
            schedule["entries"],
            key=lambda item: (
                item["slot"],
                item["hop"],
                item["arrival_slot"],
                item["id"],
            ),
        ):
            key = (entry["demand"], entry["from"])
            if entry["from"] == route_source[entry["net"]]:
                value = reference_values[entry["net"]]
            else:
                if key not in shadow:
                    raise ValidationError(
                        f"schedule consumes unavailable shadow value {key}"
                    )
                value = shadow[key]
            shadow[(entry["demand"], entry["to"])] = value

        overrides: Dict[Tuple[str, str], int] = {}
        demand_by_net = {
            route["net"]: route["id"] for route in schedule["routes"]
        }
        for net in ir.value["nets"]:
            demand = demand_by_net.get(net["id"])
            if demand is None:
                continue
            source_fpga = route_source[net["id"]]
            for endpoint in net["sinks"]:
                instance_id = endpoint["instance"]
                if instance_id is None:
                    continue
                fpga_id = assignment_map[instance_id]
                if fpga_id in route_by_net[net["id"]]["sinks"]:
                    key = (demand, fpga_id)
                    if key not in shadow:
                        raise ValidationError(
                            f"cut sink {instance_id!r} lacks shadow {key}"
                        )
                    overrides[(instance_id, net["id"])] = shadow[key]

        _, partition_next, partition_outputs = model.evaluate(
            state, cycle, seed, overrides=overrides
        )
        for record in replica_records:
            target = record["target_fpga"]
            members = {
                item["original_instance"] for item in record["instances"]
            }
            replica_overrides: Dict[Tuple[str, str], int] = {}
            for instance_id in members:
                for net_id in sink_nets_by_instance[instance_id]:
                    route = route_by_net.get(net_id)
                    if route is None or target not in route["sinks"]:
                        continue
                    demand = route["id"]
                    key = (demand, target)
                    if key not in shadow:
                        raise ValidationError(
                            f"replica {record['cluster']!r} at {target!r} "
                            f"lacks shadow {key}"
                        )
                    replica_overrides[(instance_id, net_id)] = shadow[key]
            replica_values = model.evaluate_lut_subset(
                members,
                reference_values,
                replica_overrides,
            )
            output_nets = {
                net_id
                for instance_id in members
                for net_id in output_nets_by_instance[instance_id]
            }
            for net_id in output_nets:
                if replica_values.get(net_id) != reference_values.get(net_id):
                    raise ValidationError(
                        f"cycle {cycle}: replica {record['cluster']!r} at "
                        f"{target!r} mismatches net {net_id!r}"
                    )
                compared_replica_outputs += 1
        if partition_next != reference_next:
            mismatch = next(
                instance_id
                for instance_id in sorted(reference_next)
                if partition_next.get(instance_id)
                != reference_next[instance_id]
            )
            raise ValidationError(
                f"cycle {cycle}: partition state mismatch at {mismatch!r}"
            )
        if partition_outputs != reference_outputs:
            raise ValidationError(
                f"cycle {cycle}: partition top-output mismatch"
            )
        compared_outputs += len(reference_outputs)
        compared_state_bits += model.state_bit_count()
        trace.update(
            (
                f"{cycle}:"
                + json.dumps(reference_next, sort_keys=True)
                + ":"
                + "".join(
                    str(reference_outputs[item])
                    for item in sorted(reference_outputs)
                )
            ).encode("utf-8")
        )
        state = reference_next

    return {
        "status": "pass",
        "provider": "generic-lut-ff-vtr-hard-block-cycle-model-v3",
        "cycles": cycles,
        "seed": seed,
        "primitive_instances": len(model.instances),
        "flip_flops": len(model.ff_ids),
        "luts": len(model.lut_ids),
        "multipliers": len(model.multiply_ids),
        "memory_macros": len(model.ram_ids),
        "memory_semantics": "synchronous-read-old-zero-initial-sparse",
        "register_input_cuts": sum(
            cut_class == "register_input"
            for cut_class in cut_class_by_net.values()
        ),
        "transport_rounds": len(completion_by_round),
        "round_barrier_checks": round_barrier_checks,
        "compared_state_bits": compared_state_bits,
        "compared_output_bits": compared_outputs,
        "replica_copies": len(replica_records),
        "compared_replica_output_bits": compared_replica_outputs,
        "mismatches": 0,
        "trace_sha256": trace.hexdigest(),
    }
