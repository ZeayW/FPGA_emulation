import json
import tempfile
import unittest
from pathlib import Path

from emuflow.equivalence import _MappedModel
from emuflow.errors import ValidationError
from emuflow.yosys import import_yosys_json


class XilinxEquivalencePrimitiveTest(unittest.TestCase):
    def _model(self, name, ports, cells):
        netnames = {
            port_name: {"bits": definition["bits"]}
            for port_name, definition in ports.items()
        }
        value = {
            "modules": {
                name: {
                    "attributes": {"top": "1"},
                    "ports": ports,
                    "cells": cells,
                    "netnames": netnames,
                }
            }
        }
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        source = Path(temporary_directory.name) / f"{name}.json"
        source.write_text(json.dumps(value), encoding="utf-8")
        return _MappedModel(import_yosys_json(source, top=name, clocks=[]))

    @staticmethod
    def _input_values(port, width, value):
        return {
            (port, bit): (value >> bit) & 1
            for bit in range(width)
        }

    @staticmethod
    def _output_value(outputs, port, width):
        return sum(outputs[f"{port}[{bit}]"] << bit for bit in range(width))

    def test_muxf7_muxf8_muxf9_select_inputs(self):
        for cell_type in ("MUXF7", "MUXF8", "MUXF9"):
            with self.subTest(cell_type=cell_type):
                model = self._model(
                    cell_type.lower(),
                    {
                        "i0": {"direction": "input", "bits": [2]},
                        "i1": {"direction": "input", "bits": [3]},
                        "s": {"direction": "input", "bits": [4]},
                        "o": {"direction": "output", "bits": [5]},
                    },
                    {
                        "mux": {
                            "type": cell_type,
                            "parameters": {},
                            "attributes": {},
                            "port_directions": {
                                "I0": "input",
                                "I1": "input",
                                "S": "input",
                                "O": "output",
                            },
                            "connections": {
                                "I0": [2],
                                "I1": [3],
                                "S": [4],
                                "O": [5],
                            },
                        }
                    },
                )
                for select, expected in ((0, 0), (1, 1)):
                    _values, _next_state, outputs = model.evaluate(
                        model.initial_state(),
                        cycle=0,
                        seed=1,
                        input_values={
                            ("i0", 0): 0,
                            ("i1", 0): 1,
                            ("s", 0): select,
                        },
                    )
                    self.assertEqual(outputs["o[0]"], expected)

    def test_lut6_2_has_distinct_o5_and_o6_addresses(self):
        input_bits = list(range(2, 8))
        model = self._model(
            "lut6_2",
            {
                "i": {"direction": "input", "bits": input_bits},
                "o5": {"direction": "output", "bits": [8]},
                "o6": {"direction": "output", "bits": [9]},
            },
            {
                "lut": {
                    "type": "LUT6_2",
                    "parameters": {"INIT": format(1 << 3, "064b")},
                    "attributes": {},
                    "port_directions": {
                        **{f"I{index}": "input" for index in range(6)},
                        "O5": "output",
                        "O6": "output",
                    },
                    "connections": {
                        **{
                            f"I{index}": [input_bits[index]]
                            for index in range(6)
                        },
                        "O5": [8],
                        "O6": [9],
                    },
                }
            },
        )
        inputs = self._input_values("i", 6, 0b100011)
        _values, _next_state, outputs = model.evaluate(
            model.initial_state(), 0, 1, input_values=inputs
        )
        self.assertEqual(outputs["o5[0]"], 1)
        self.assertEqual(outputs["o6[0]"], 0)

    def test_carry8_matches_xilinx_single_and_dual_chain_semantics(self):
        ports = {
            "ci": {"direction": "input", "bits": [2]},
            "ci_top": {"direction": "input", "bits": [3]},
            "di": {"direction": "input", "bits": list(range(4, 12))},
            "s": {"direction": "input", "bits": list(range(12, 20))},
            "co": {"direction": "output", "bits": list(range(20, 28))},
            "o": {"direction": "output", "bits": list(range(28, 36))},
        }
        ci, ci_top, data, select = 1, 0, 0b10101100, 0b11010110
        for carry_type in ("SINGLE_CY8", "DUAL_CY4"):
            with self.subTest(carry_type=carry_type):
                model = self._model(
                    f"carry_{carry_type.lower()}",
                    ports,
                    {
                        "carry": {
                            "type": "CARRY8",
                            "parameters": {"CARRY_TYPE": carry_type},
                            "attributes": {},
                            "port_directions": {
                                "CI": "input",
                                "CI_TOP": "input",
                                "DI": "input",
                                "S": "input",
                                "CO": "output",
                                "O": "output",
                            },
                            "connections": {
                                "CI": [2],
                                "CI_TOP": [3],
                                "DI": list(range(4, 12)),
                                "S": list(range(12, 20)),
                                "CO": list(range(20, 28)),
                                "O": list(range(28, 36)),
                            },
                        }
                    },
                )
                inputs = {("ci", 0): ci, ("ci_top", 0): ci_top}
                inputs.update(self._input_values("di", 8, data))
                inputs.update(self._input_values("s", 8, select))
                _values, _next_state, outputs = model.evaluate(
                    model.initial_state(), 0, 1, input_values=inputs
                )
                carry = ci
                expected_co = 0
                expected_o = 0
                for bit in range(8):
                    if bit == 4 and carry_type == "DUAL_CY4":
                        carry = ci_top
                    expected_o |= (((select >> bit) & 1) ^ carry) << bit
                    carry = (
                        carry
                        if (select >> bit) & 1
                        else (data >> bit) & 1
                    )
                    expected_co |= carry << bit
                self.assertEqual(
                    self._output_value(outputs, "co", 8), expected_co
                )
                self.assertEqual(
                    self._output_value(outputs, "o", 8), expected_o
                )

    def test_dsp48e2_matches_yosys_combinational_signed_multiply(self):
        a_bits = list(range(2, 32))
        b_bits = list(range(32, 50))
        p_bits = list(range(50, 98))
        parameters = {
            name: "0"
            for name in (
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
            )
        }
        parameters.update({
            "A_INPUT": "DIRECT",
            "B_INPUT": "DIRECT",
            "USE_MULT": "MULTIPLY",
            "USE_SIMD": "ONE48",
            "AMULTSEL": "A",
            "BMULTSEL": "B",
        })
        connections = {
            "A": a_bits,
            "B": b_bits,
            "P": p_bits,
            "INMODE": ["0"] * 5,
            "ALUMODE": ["0"] * 4,
            "OPMODE": ["1", "0", "1"] + ["0"] * 6,
            "CARRYINSEL": ["0"] * 3,
            "CARRYIN": ["0"],
        }
        model = self._model(
            "dsp",
            {
                "a": {"direction": "input", "bits": a_bits},
                "b": {"direction": "input", "bits": b_bits},
                "p": {"direction": "output", "bits": p_bits},
            },
            {
                "multiply": {
                    "type": "DSP48E2",
                    "parameters": parameters,
                    "attributes": {},
                    "port_directions": {
                        key: "output" if key == "P" else "input"
                        for key in connections
                    },
                    "connections": connections,
                }
            },
        )
        left = (-3) & ((1 << 27) - 1)
        right = 5
        inputs = self._input_values("a", 30, left)
        inputs.update(self._input_values("b", 18, right))
        _values, _next_state, outputs = model.evaluate(
            model.initial_state(), 0, 1, input_values=inputs
        )
        self.assertEqual(
            self._output_value(outputs, "p", 48),
            (-15) & ((1 << 48) - 1),
        )

    def _ramb18_model(self, *, initialized=False):
        address = list(range(2, 16))
        data = list(range(16, 48))
        parity = list(range(48, 52))
        write_enable = list(range(52, 56))
        dout = list(range(56, 88))
        doutp = list(range(88, 92))
        parameters = {
            "DOA_REG": "0",
            "DOB_REG": "0",
            "READ_WIDTH_A": "36",
            "READ_WIDTH_B": "0",
            "WRITE_WIDTH_A": "0",
            "WRITE_WIDTH_B": "36",
            "WRITE_MODE_A": "READ_FIRST",
            "WRITE_MODE_B": "READ_FIRST",
            "INIT_A": "0",
            "INIT_B": "0",
            "SRVAL_A": "0",
            "SRVAL_B": "0",
            "INIT_00": "1" if initialized else "x" * 256,
        }
        connections = {
            "ADDRARDADDR": address,
            "ADDRBWRADDR": address,
            "DINADIN": data[:16],
            "DINPADINP": parity[:2],
            "DINBDIN": data[16:],
            "DINPBDINP": parity[2:],
            "WEA": ["0"] * 2,
            "WEBWE": write_enable,
            "ENARDEN": ["1"],
            "ENBWREN": ["1"],
            "SLEEP": ["0"],
            "RSTRAMARSTRAM": ["0"],
            "RSTRAMB": ["0"],
            "DOUTADOUT": dout[:16],
            "DOUTPADOUTP": doutp[:2],
            "DOUTBDOUT": dout[16:],
            "DOUTPBDOUTP": doutp[2:],
        }
        return self._model(
            "bram",
            {
                "address": {"direction": "input", "bits": address},
                "data": {"direction": "input", "bits": data},
                "parity": {"direction": "input", "bits": parity},
                "write_enable": {
                    "direction": "input",
                    "bits": write_enable,
                },
                "dout": {"direction": "output", "bits": dout},
                "doutp": {"direction": "output", "bits": doutp},
            },
            {
                "memory": {
                    "type": "RAMB18E2",
                    "parameters": parameters,
                    "attributes": {},
                    "port_directions": {
                        key: "output" if key.startswith("DOUT") else "input"
                        for key in connections
                    },
                    "connections": connections,
                }
            },
        )

    def test_ramb18e2_models_yosys_sdp_read_first_behavior(self):
        model = self._ramb18_model()
        state = model.initial_state()
        data = 0xC33CA55A
        parity = 0b1010
        inputs = self._input_values("address", 14, 0x20)
        inputs.update(self._input_values("data", 32, data))
        inputs.update(self._input_values("parity", 4, parity))
        inputs.update(self._input_values("write_enable", 4, 0b1111))
        _values, state, outputs = model.evaluate(
            state, 0, 1, input_values=inputs
        )
        self.assertEqual(self._output_value(outputs, "dout", 32), 0)
        inputs.update(self._input_values("write_enable", 4, 0))
        _values, state, outputs = model.evaluate(
            state, 1, 1, input_values=inputs
        )
        self.assertEqual(self._output_value(outputs, "dout", 32), 0)
        _values, _state, outputs = model.evaluate(
            state, 2, 1, input_values=inputs
        )
        self.assertEqual(self._output_value(outputs, "dout", 32), data)
        self.assertEqual(self._output_value(outputs, "doutp", 4), parity)

    def test_ramb18e2_nonzero_initial_contents_fail_closed(self):
        model = self._ramb18_model(initialized=True)
        with self.assertRaisesRegex(ValidationError, "initialized contents"):
            model.initial_state()


if __name__ == "__main__":
    unittest.main()
