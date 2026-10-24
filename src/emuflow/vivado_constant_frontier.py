"""Bounded functional checks for a vendor's conditional constant selection.

An absent register and a ground pin with the same spelling do not establish
this fact. Compare the entire retained LUT function, including its INIT and
the provenance of every dynamic input, so pin permutations cannot masquerade
as evidence of a constant choice. This module assigns no physical delay.
"""
from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from .errors import ValidationError


def _init_bits(value: str, width: int) -> int:
    if not isinstance(value, str):
        raise ValidationError("constant-frontier LUT INIT is invalid")
    native = re.fullmatch(r"([0-9]+)'([hHbB])([0-9a-fA-F]+)", value)
    if native:
        if int(native[1]) != 1 << width:
            raise ValidationError("constant-frontier LUT INIT width differs")
        try:
            result = int(native[3], 16 if native[2].lower() == "h" else 2)
        except ValueError as error:
            raise ValidationError("constant-frontier LUT INIT is invalid") from error
    elif len(value) == 1 << width and set(value) <= {"0", "1"}:
        result = int(value, 2)
    else:
        raise ValidationError("constant-frontier LUT INIT is invalid")
    if result >= 1 << (1 << width):
        raise ValidationError("constant-frontier LUT INIT exceeds its width")
    return result


def _lut(function: Mapping[str, Any]):
    kind = function.get("type")
    if kind not in {f"LUT{i}" for i in range(1, 7)}:
        raise ValidationError("constant-frontier check requires a single-output LUT")
    width = int(kind[-1])
    pins = function.get("inputs")
    if not isinstance(pins, Mapping) or set(pins) != {f"I{i}" for i in range(width)}:
        raise ValidationError("constant-frontier LUT input coverage differs")
    if any(isinstance(x, bool) or not (isinstance(x, int) and x in (0, 1)
                                      or isinstance(x, str) and x) for x in pins.values()):
        raise ValidationError("constant-frontier LUT input identity is invalid")
    return width, _init_bits(function.get("init"), width), pins


def require_equal_frontier_functions(source: Mapping[str, Any], native: Mapping[str, Any]) -> dict[str, int]:
    """Exhaust all assignments to at most twelve primitive input variables.

    Variable strings must already identify checked source-origin output pins
    or explicitly unrelated native objects. They are never inferred from a
    coincidentally matching physical input pin name.
    """
    left, right = _lut(source), _lut(native)
    variables = sorted({value for _, _, pins in (left, right) for value in pins.values()
                        if isinstance(value, str)})
    if len(variables) > 12:
        raise ValidationError("constant-frontier function exceeds its bounded primitive scope")
    indices = {name: i for i, name in enumerate(variables)}

    def value(function, assignment):
        width, init, pins = function
        index = 0
        for i in range(width):
            binding = pins[f"I{i}"]
            bit = binding if isinstance(binding, int) else assignment >> indices[binding] & 1
            index |= bit << i
        return init >> index & 1

    for assignment in range(1 << len(variables)):
        if value(left, assignment) != value(right, assignment):
            raise ValidationError("native frontier function does not confirm the source constant selection")
    return {"variables": len(variables), "assignments": 1 << len(variables)}
