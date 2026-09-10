"""Read nextpnr's non-CVC SDF and bind it to the packed physical netlist.

This is delay annotation, not STA. It preserves rise/fall min/typ/max and
setup/hold separately. Missing arcs are never assigned a zero delay.
"""
import math
import re
from .errors import ValidationError


def _forms(text):
    tokens = re.findall(r'"(?:\\.|[^"\\])*"|[()]|(?:\\.|[^\s()])+', text)
    stack = []; root = []
    for token in tokens:
        if token == "(":
            child = []
            (stack[-1] if stack else root).append(child)
            stack.append(child)
        elif token == ")":
            if not stack: raise ValidationError("unbalanced SDF")
            stack.pop()
        else:
            if not stack: raise ValidationError("SDF token outside form")
            stack[-1].append(token)
    if stack or len(root) != 1 or not root[0] or root[0][0] != "DELAYFILE":
        raise ValidationError("invalid SDF root")
    return root[0][1:]


def _name(value):
    if not isinstance(value, str): raise ValidationError("invalid SDF name")
    if value.startswith('"'):
        if not value.endswith('"'): raise ValidationError("invalid quoted SDF name")
        value = value[1:-1]
    return re.sub(r'\\(.)', r'\1', value)


def _triple(form, scale):
    if not isinstance(form, list) or len(form) != 1 or not isinstance(form[0], str):
        raise ValidationError("invalid SDF delay triple")
    try: values = tuple(float(v) * scale for v in form[0].split(":"))
    except ValueError as exc: raise ValidationError("nonnumeric SDF delay") from exc
    if len(values) != 3 or not all(math.isfinite(v) for v in values) or not values[0] <= values[1] <= values[2]:
        raise ValidationError("incomplete or unordered SDF delay triple")
    return values


def read_nextpnr_sdf(text, routed, *, top="top"):
    """Return checked primitive/wire annotations in ns for non-CVC nextpnr SDF.

    Cell IDs and port bits must match the routed file, not pre-pack Yosys IDs.
    Setup/hold values may be negative; propagation delays may not. No timing
    completeness, false-path, clock-domain or global WNS claim is implied.
    """
    forms = _forms(text); headers = {}; cell_forms = []
    for form in forms:
        if not isinstance(form, list) or not form: raise ValidationError("invalid SDF form")
        if form[0] == "CELL": cell_forms.append(form[1:])
        else:
            if form[0] in headers: raise ValidationError("duplicate SDF header")
            headers[form[0]] = form[1:]
    if headers.get("DIVIDER") != ["/"] or headers.get("TIMESCALE") != ["1ps"] or headers.get("PROGRAM") != ['"nextpnr"']:
        raise ValidationError("unsupported SDF producer, divider or timescale")
    module = routed.get("modules", {}).get(top)
    if not isinstance(module, dict): raise ValidationError("missing routed SDF top")
    cells = module.get("cells", {}); annotations = {}; wires = {}

    def endpoint(name, *, escaped=True):
        try: cell, port = (_name(name) if escaped else name).rsplit("/", 1)
        except ValueError as exc: raise ValidationError("invalid SDF endpoint") from exc
        if cell not in cells: raise ValidationError(f"unknown SDF cell: {cell}")
        bits = cells[cell].get("connections", {}).get(port, [])
        if len(bits) != 1: raise ValidationError(f"unconnected SDF port: {cell}/{port}")
        return (cell, port), bits[0]

    root_seen = False
    for items in cell_forms:
        fields = {}
        for item in items:
            if not isinstance(item, list) or not item or item[0] in fields:
                raise ValidationError("invalid or duplicate SDF cell field")
            fields[item[0]] = item[1:]
        if set(fields) - {"CELLTYPE", "INSTANCE", "DELAY", "TIMINGCHECK"}:
            raise ValidationError("unsupported SDF cell field")
        instance = fields.get("INSTANCE"); kind = fields.get("CELLTYPE")
        if instance is None or len(instance) > 1 or not kind or len(kind) != 1:
            raise ValidationError("missing SDF cell identity")
        name = _name(instance[0]) if instance else ""
        if not name:
            if root_seen: raise ValidationError("duplicate SDF top")
            root_seen = True
        elif name not in cells or cells[name].get("type") != _name(kind[0]):
            raise ValidationError(f"SDF primitive mismatch: {name}")
        if name in annotations: raise ValidationError("duplicate SDF instance")
        record = {"iopaths": {}, "setuphold": {}}
        annotations[name] = record
        delay = fields.get("DELAY", [])
        if delay:
            if len(delay) != 1 or not delay[0] or delay[0][0] != "ABSOLUTE":
                raise ValidationError("only absolute SDF delays are supported")
            for arc in delay[0][1:]:
                if len(arc) != 5 or arc[0] not in {"IOPATH", "INTERCONNECT"}:
                    raise ValidationError("unsupported SDF propagation arc")
                values = (_triple(arc[3], .001), _triple(arc[4], .001))
                if any(v < 0 for triple in values for v in triple):
                    raise ValidationError("negative propagation delay")
                if arc[0] == "INTERCONNECT":
                    if name: raise ValidationError("non-top SDF interconnect")
                    source, sb = endpoint(arc[1]); sink, tb = endpoint(arc[2])
                    if type(sb) is not int or sb != tb:
                        raise ValidationError("SDF interconnect disagrees with routed connectivity")
                    key = (source, sink); target = wires
                else:
                    if not name: raise ValidationError("top-level SDF IOPATH")
                    source, _ = endpoint(name + "/" + _name(arc[1]), escaped=False)
                    sink, _ = endpoint(name + "/" + _name(arc[2]), escaped=False)
                    key = (source[1], sink[1]); target = record["iopaths"]
                if key in target: raise ValidationError("duplicate SDF propagation arc")
                target[key] = values
        for check in fields.get("TIMINGCHECK", []):
            if len(check) != 5 or check[0] != "SETUPHOLD" or not name:
                raise ValidationError("unsupported SDF timing check")
            edges = []
            for edge in check[1:3]:
                if len(edge) != 2 or edge[0] not in {"posedge", "negedge"}:
                    raise ValidationError("unsupported SDF check edge")
                _, _bit = endpoint(name + "/" + _name(edge[1]), escaped=False)
                edges.append((edge[0], _name(edge[1])))
            key = tuple(edges)
            if key in record["setuphold"]: raise ValidationError("duplicate SDF setuphold")
            record["setuphold"][key] = (_triple(check[3], .001), _triple(check[4], .001))
    if not root_seen or set(annotations) - {""} != set(cells):
        raise ValidationError("SDF does not account for every routed primitive")
    del annotations[""]
    return {"cells": annotations, "interconnect": wires, "units": "ns",
            "delay_connectivity_checked": True, "timing_path_coverage_qualified": False,
            "global_timing_qualified": False}
