"""Explicit initial-state contract for a mapped LUT/FF snapshot experiment.

Undefined power-up state is not a reset value. A caller must explicitly choose
a seed for those bits; this qualifies that starting state, not all DUT states.
Source-defined initial bits always take precedence and conflicting aliases fail.
"""
import random

from .errors import ValidationError


def mapped_snapshot_initial_state(mapped: dict, *, top: str, undefined_seed: int):
    if type(undefined_seed) is not int or undefined_seed < 0:
        raise ValidationError("undefined-state seed must be an explicit nonnegative integer")
    module = mapped.get("modules", {}).get(top)
    if not isinstance(module, dict):
        raise ValidationError("mapped initial-state contract needs the selected top module")
    known = {}
    for net in module.get("netnames", {}).values():
        value = net.get("attributes", {}).get("init")
        if value is None:
            continue
        bits = net.get("bits", [])
        if (not isinstance(value, str) or len(value) != len(bits)
                or any(c not in "01x" for c in value)):
            raise ValidationError("malformed mapped init vector")
        # Yosys bit arrays are LSB-first; parameter strings are MSB-first.
        for bit, digit in zip(bits, reversed(value)):
            if digit == "x":
                continue
            if type(bit) is not int:
                if bit != digit:
                    raise ValidationError("init contradicts a constant net")
                continue
            number = int(digit)
            if bit in known and known[bit] != number:
                raise ValidationError("conflicting source initial-state aliases")
            known[bit] = number
    rng = random.Random(undefined_seed)
    state = {}; source_defined = 0; driven = set()
    for name, cell in sorted(module.get("cells", {}).items()):
        kind = cell.get("type")
        if kind == "$lut":
            continue
        if kind != "$_DFF_P_":
            raise ValidationError(f"unsupported mapped initial-state primitive: {kind}")
        output = cell.get("connections", {}).get("Q", [])
        if len(output) != 1 or type(output[0]) is not int or output[0] in driven:
            raise ValidationError("snapshot FF requires one unique driven Q bit")
        bit = output[0]; driven.add(bit)
        if bit in known:
            state[name] = known[bit]; source_defined += 1
        else:
            state[name] = rng.getrandbits(1)
    return state, {
        "schema": "emuflow.snapshot-initial-state/v1",
        "source_defined_bits": source_defined,
        "explicitly_seeded_undefined_bits": len(state) - source_defined,
        "undefined_seed": undefined_seed,
        "scope": "declared-starting-state-only",
        "universal_reset_or_initial_state_proof": False,
    }
