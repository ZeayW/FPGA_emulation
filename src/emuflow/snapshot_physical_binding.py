"""Resolve emitted DUT identities in nextpnr's routed Yosys JSON.

Connectivity provenance only; not an SDF delay reader or a timing certificate.
Unresolved aliases fail rather than falling back to name suffix guesses.
"""
from .errors import ValidationError


def bind_snapshot_routed_identities(source_binding, routed, *, hierarchy, top="top"):
    """Resolve every requested one-bit source alias, including merged aliases.

    The exact hierarchy is supplied by the composing top. Constants may be a
    legitimate optimization, but remain explicitly distinguished from FFs.
    Multiple original registers may resolve to one physical FF. Neither case
    proves sequential equivalence, which is a separate qualification obligation.
    """
    if (source_binding.get("schema") != "emuflow.snapshot-source-binding/v1"
            or not isinstance(hierarchy, str) or not hierarchy or hierarchy.endswith(".")):
        raise ValidationError("invalid snapshot source binding or hierarchy")
    module = routed.get("modules", {}).get(top)
    if not isinstance(module, dict):
        raise ValidationError("missing routed top module")
    nets, cells = module.get("netnames", {}), module.get("cells", {})
    q_drivers = {}
    for name, cell in cells.items():
        if cell.get("type") != "TRELLIS_FF":
            continue
        q = cell.get("connections", {}).get("Q", [])
        if len(q) != 1 or type(q[0]) is not int or q[0] < 0:
            raise ValidationError("malformed routed FF Q")
        q_drivers.setdefault(q[0], []).append(name)
    resolved = {}
    for category in ("nets", "registers"):
        aliases = source_binding.get(category)
        if not isinstance(aliases, dict):
            raise ValidationError("missing source alias table")
        records = {}
        for original, local in aliases.items():
            if not isinstance(local, str) or not local:
                raise ValidationError("invalid source alias")
            full = hierarchy + "." + local
            bits = nets.get(full, {}).get("bits", [])
            if len(bits) != 1 or not ((type(bits[0]) is int and bits[0] >= 0) or bits[0] in ("0", "1")):
                raise ValidationError(f"unresolved routed source alias: {full}")
            bit = bits[0]
            record = {"alias": full, "bit": bit}
            if category == "registers":
                if type(bit) is str:
                    record.update(kind="constant", value=int(bit))
                else:
                    drivers = q_drivers.get(bit, [])
                    if len(drivers) != 1:
                        raise ValidationError(f"source state has no unique physical FF: {full}")
                    record.update(kind="ff", cell=drivers[0], port="Q")
            records[original] = record
        resolved[category] = records
    return {"schema":"emuflow.snapshot-routed-identities/v1", **resolved,
            "delay_annotation_qualified":False,"global_timing_qualified":False}
