"""Resolve emitted DUT identities in nextpnr's routed Yosys JSON.

Connectivity provenance only; not an SDF delay reader or a timing certificate.
Unresolved aliases fail rather than falling back to name suffix guesses.
"""
from .errors import ValidationError


def bind_snapshot_routed_identities(source_binding, routed, *, hierarchy, top="top",
                                   mapped=None, mapped_top=None, port_bindings=None):
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
    # nextpnr keeps a canonical name per wire, not every Yosys alias. Bridge
    # through actual equal-bit aliases in the PRE-pack netlist. Bit numbers
    # are local to each file and must never be equated across the two files.
    mapped_nets = {}; aliases_by_bit = {}
    if mapped is not None:
        pre = mapped.get("modules", {}).get(mapped_top)
        if not isinstance(pre, dict):
            raise ValidationError("missing explicit mapped top module")
        mapped_nets = pre.get("netnames", {})
        for name, item in mapped_nets.items():
            bits = item.get("bits", [])
            if item.get("upto", 0):
                continue  # Ascending bus spelling needs a separately checked adapter.
            offset = item.get("offset", 0)
            if type(offset) is not int:
                raise ValidationError("invalid mapped bus offset")
            for index, bit in enumerate(bits):
                if type(bit) is int:
                    aliases_by_bit.setdefault(bit, []).append((name, index, len(bits), offset))

    port_bindings = {} if port_bindings is None else port_bindings
    if not isinstance(port_bindings, dict) or any(not isinstance(v,str) or not v for v in port_bindings.values()):
        raise ValidationError("invalid physical port bindings")

    def resolve(full, references):
        direct = nets.get(full, {}).get("bits", [])
        found = set()
        if len(direct) == 1:
            found.add(direct[0])
        prebits=[]
        before = mapped_nets.get(full, {}).get("bits", [])
        if len(before) == 1: prebits.append(before[0])
        for ref in references:
            port, index = ref.get("port"), ref.get("bit")
            if not isinstance(port,str) or type(index) is not int or index<0:
                raise ValidationError("invalid source port-bit reference")
            name=port_bindings.get(port,hierarchy+"."+port)
            vector=nets.get(name,{}).get("bits",[])
            if index<len(vector): found.add(vector[index])
            before=mapped_nets.get(name,{}).get("bits",[])
            if index<len(before): prebits.append(before[index])
        for prebit in prebits:
            if prebit in ("0", "1"):
                found.add(prebit)
            for name, index, width, offset in aliases_by_bit.get(prebit, []):
                # Consume a vector if retained, otherwise its exact split-bit
                # spelling. All candidates originate in the mapped JSON.
                vector = nets.get(name, {}).get("bits", [])
                if len(vector) == width:
                    found.add(vector[index])
                split = nets.get(f"{name}[{offset+index}]", {}).get("bits", [])
                if len(split) == 1:
                    found.add(split[0])
        if len(found) != 1:
            raise ValidationError(f"unresolved or conflicting routed source alias: {full}")
        bit = found.pop()
        if not ((type(bit) is int and bit >= 0) or bit in ("0", "1")):
            raise ValidationError(f"unknown routed source bit: {full}")
        return bit
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
            references=source_binding.get("net_ports",{}).get(original,[]) if category=="nets" else []
            bit = resolve(full,references)
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
