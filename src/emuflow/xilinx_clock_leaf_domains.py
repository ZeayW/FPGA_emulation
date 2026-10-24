"""Explicit qualification of physical and declared ideal FF clock leaf loads."""
from collections import defaultdict
import json
import os
from pathlib import Path
import re
import tempfile

from .errors import ValidationError
from .io import file_sha256
from .xilinx_netlist import open_xilinx_mapped_index


def complete_ideal_ff_clock_leaf_loads(
    audit_path: Path, route_input_path: Path, mapped_path: Path, output_path: Path,
):
    """Seal a separate domain payload including source-declared ideal FF clocks.

    These are capacity reservations, not physically routed boundary clocks.
    The current backend's ideal-boundary-clock contract remains unchanged.
    """
    audit_path, route_input_path, mapped_path, output_path = map(
        Path, (audit_path, route_input_path, mapped_path, output_path)
    )
    if output_path.resolve() == audit_path.resolve():
        raise ValidationError("ideal clock augmentation must publish separately")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    payload = audit.get("placement_domain", {})
    name = payload.get("path")
    if (audit.get("schema") != "emuflow.clock-leaf-domain-audit/v1"
            or audit.get("input_sha256") != file_sha256(route_input_path)
            or audit.get("source", {}).get("mapped_sha256") != file_sha256(mapped_path)
            or not isinstance(name, str) or Path(name).name != name
            or payload.get("schema") != "emuflow.clock-leaf-placement-domain/v1"):
        raise ValidationError("ideal clock leaf augmentation source identity differs")
    source_payload = audit_path.parent / name
    if file_sha256(source_payload) != payload.get("sha256"):
        raise ValidationError("ideal clock leaf augmentation payload hash differs")
    clocks = {}
    capacities = {}
    fixed = defaultdict(set)
    with source_payload.open(encoding="utf-8") as stream:
        for line in stream:
            row = line.split()
            if row[0] == "C":
                key = (row[1], row[2])
                if key in clocks:
                    raise ValidationError("duplicate source clock leaf pin")
                clocks[key] = row[3]
            elif row[0] == "D":
                capacities[int(row[1])] = int(row[2])
            elif row[0] == "P":
                fixed[int(row[1])].add(row[2])
    ideal_nets = set()
    with route_input_path.open(encoding="utf-8") as stream:
        for line in stream:
            row = line.rstrip("\n").split("\t")
            if (len(row) == 4 and row[:1] == ["EXCLUDED"]
                    and row[2:] == ["boundary_clock", "ideal-boundary-clock"]):
                ideal_nets.add(row[1])
    additions = {}
    with open_xilinx_mapped_index(mapped_path) as mapped:
        modules = mapped["modules"]
        top = next(iter(modules))
        cells = modules[top]["cells"]
        with route_input_path.open(encoding="utf-8") as stream:
            for line in stream:
                row = line.rstrip("\n").split("\t")
                if (row[0] != "CELL" or row[3] not in {"FDRE", "FDCE", "FDSE", "FDPE"}
                        or not row[4].startswith("SLICE_")):
                    continue
                if not re.fullmatch(r"[A-H]FF2?", row[5]):
                    raise ValidationError("unqualified ideal FF BEL")
                key = (row[4], "CLK1" if row[5][0] < "E" else "CLK2")
                if key in clocks:
                    continue
                bit = cells[row[2]]["connections"].get("C")
                if not isinstance(bit, list) or len(bit) != 1 or not isinstance(bit[0], int):
                    raise ValidationError("missing source identity for ideal FF clock")
                clock = f"n{bit[0]}"
                if clock not in ideal_nets:
                    raise ValidationError("unmodeled FF clock is not a declared ideal boundary clock")
                if key in additions and additions[key] != clock:
                    raise ValidationError("ideal FF clock control-set conflict")
                additions[key] = clock
    all_clocks = dict(clocks)
    all_clocks.update(additions)
    relevant_sites = {site for site, _pin in all_clocks}
    domains = {}
    with source_payload.open(encoding="utf-8") as stream:
        for line in stream:
            row = line.split()
            if row[0] == "S" and row[1] in relevant_sites:
                domains[(row[1], "CLK1")] = int(row[2])
                domains[(row[1], "CLK2")] = int(row[3])
    loads = defaultdict(set, {bank: set(values) for bank, values in fixed.items()})
    examples = {}
    for key, clock in all_clocks.items():
        if key not in domains:
            raise ValidationError("FF clock pin has no source-sealed leaf domain")
        bank = domains[key]
        loads[bank].add(clock)
        examples.setdefault(bank, f"IN {key[0]}.{key[1]}")
    overfull = sorted(
        (bank for bank in loads if len(loads[bank]) > capacities[bank]),
        key=lambda bank: (capacities[bank]-len(loads[bank]), bank),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    target_payload = output_path.with_name(output_path.name + ".domains.tsv")
    if target_payload.resolve() == source_payload.resolve():
        raise ValidationError("ideal clock augmentation must publish separately")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output_path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            with source_payload.open(encoding="utf-8") as original:
                for line in original:
                    stream.write(line)
            for (site, pin), clock in sorted(additions.items()):
                stream.write(f"C\t{site}\t{pin}\t{clock}\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, target_payload)
        finally:
            if temporary.exists():
                temporary.unlink()
    result = dict(audit)
    result.update(
        qualification="ordinary-physical-clock-and-declared-ideal-ff-leaf-demand-only",
        oversubscribed_domains=len(overfull),
        maximum_excess_clocks=max((len(loads[bank])-capacities[bank] for bank in overfull), default=0),
        domains=len(loads),
        examples=[dict(domain=bank, clocks=len(loads[bank]), local_capacity=capacities[bank],
                       clock_nets=sorted(loads[bank]), example_pin=examples.get(bank))
                  for bank in overfull[:16]],
        ideal_boundary_ff_clock_nets=sorted({clock for clock in all_clocks.values() if clock in ideal_nets}),
        ideal_boundary_ff_clock_pins=sum(clock in ideal_nets for clock in all_clocks.values()),
        physical_clock_nets=audit["clock_nets"],
        augmentation=dict(source_audit_sha256=file_sha256(audit_path),
                          policy="mapped-ff-clocks-declared-ideal-boundary-only-v1"),
    )
    result["placement_domain"] = dict(payload, path=target_payload.name,
        sha256=file_sha256(target_payload), slice_clock_pins=len(all_clocks),
        ideal_boundary_ff_clock_pins=result["ideal_boundary_ff_clock_pins"])
    from .io import write_json
    write_json(output_path, result, compact=True)
    return result
