"""Read-only streaming timing view of compact EmuIR, without a Python graph."""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

from .emuir_compact import _decode, compact_metadata, compact_nets
from .emuir_storage import open_emuir_sqlite_read_only
from .errors import ValidationError


# Equality to one avoids integer casts: binary and decimal strings can equal
# one only after optional zero padding. Keep the native stoll ASCII whitespace,
# optional plus and decimal underscore rules; bools/reals are never parameters.
# Older SQLite truncates scalar JSON strings at NUL. The array form preserves
# its escaped spelling, so reject NUL before interpreting the extracted scalar.
_NEGATIVE_TIMING_CELLS = """
WITH timing_parameters AS (
    SELECT c.seq,c.type,json_type(p.value,'$.IS_C_INVERTED') AS kind,
           json_extract(p.value,'$.IS_C_INVERTED') AS raw,
           json_extract(p.value,'$.IS_C_INVERTED','$.IS_C_INVERTED') AS encoded
    FROM cell_records c LEFT JOIN payloads p ON p.seq=c.parameters_seq
    WHERE c.type IN ('FDCE','FDPE','FDRE','FDSE')
), timing_tokens AS (
    SELECT *,trim(raw,char(9)||char(10)||char(11)||char(12)||char(13)||' ') AS token
    FROM timing_parameters
), timing_digits AS (
    SELECT *,CASE WHEN substr(token,1,1)='+' THEN substr(token,2) ELSE token END AS digits
    FROM timing_tokens
), negative_timing_cells AS (
    SELECT seq,type FROM timing_digits
    WHERE (kind='integer' AND raw=1) OR
          (kind='text' AND instr(encoded,'\\u0000')=0 AND instr(raw,char(0))=0 AND digits<>''
           AND digits NOT GLOB '*[^0-9_]*'
           AND substr(digits,1,1)<>'_' AND substr(digits,-1,1)<>'_'
           AND instr(digits,'__')=0 AND ltrim(replace(digits,'_',''),'0')='1')
)
"""


def _timing_parameter_is_one(raw):
    if type(raw) is int:
        return raw == 1
    if not isinstance(raw, str) or not raw or "\0" in raw:
        return False
    digits = raw.strip(" \t\n\v\f\r")
    if digits.startswith("+"):
        digits = digits[1:]
    return bool(digits) and all(c in "0123456789_" for c in digits) and not (
        digits.startswith("_") or digits.endswith("_") or "__" in digits
    ) and digits.replace("_", "").lstrip("0") == "1"


def _timing_rows(connection, *, representatives=False):
    selected = ""
    prefix = ""
    if representatives:
        prefix = _NEGATIVE_TIMING_CELLS + """, representatives AS (
            SELECT min(seq) AS seq FROM cell_records GROUP BY type
            UNION SELECT min(seq) FROM negative_timing_cells GROUP BY type
        ) """
        selected = "JOIN representatives chosen ON chosen.seq=c.seq"
    return connection.execute(prefix + """SELECT c.seq,c.id,c.type,
        CASE WHEN c.type IN ('FDCE','FDPE','FDRE','FDSE') THEN p.value END AS parameters_json,
        k.value AS constants_json FROM cell_records c
        LEFT JOIN payloads p ON p.seq=c.parameters_seq
        LEFT JOIN payloads k ON k.seq=c.constants_seq """ + selected + " ORDER BY c.seq")


def _timing_instance(row):
    return {"id": row["id"], "type": row["type"],
            "parameters": {} if row["parameters_json"] is None else _decode(row["parameters_json"], "parameters"),
            "constant_connections": _decode(row["constants_json"], "constants")}


def _timing_instances(connection):
    """Do not decode resources, attributes or LUT/RAM INIT data for STA."""
    for row in _timing_rows(connection):
        yield _timing_instance(row)


class _Rows:
    def __init__(self, view, table, iterator):
        self.view, self.table, self.iterator = view, table, iterator

    def __iter__(self):
        count = 0
        for record in self.iterator(self.view.connection):
            count += 1
            yield record
        if count != self.view.counts[self.table]:
            raise ValueError("compact EmuIR record count mismatch")

    def __len__(self):
        return self.view.counts[self.table]


class _Ids:
    def __init__(self, view, table):
        self.view, self.table = view, table
        self.cache = OrderedDict()

    def __contains__(self, key):
        if not isinstance(key, str):
            return False
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        found = self.view.connection.execute(
            f"SELECT 1 FROM {self.table} WHERE id=?", (key,)
        ).fetchone() is not None
        self.cache[key] = found
        if len(self.cache) > 65536:
            self.cache.popitem(last=False)
        return found

    def __rsub__(self, values):
        return {key for key in values if key not in self}


class _Objects:
    def __init__(self, view):
        self.view = view
        self.cache = OrderedDict()

    def _canonical(self, name):
        for port in self.view.value["ports"]:
            for bit in range(port["width"]):
                names = [port["id"]] if port["width"] == 1 else [f"{port['id']}[{bit}]", f"{port['id']}__{bit}"]
                if name in names:
                    return {"object": name, "instance": None, "port": port["id"], "bit": bit}
        if "/" not in name:
            return None
        owner, scalar = name.rsplit("/", 1)
        row = self.view.connection.execute("SELECT seq FROM cell_records WHERE id=?", (owner,)).fetchone()
        if row is None:
            return None
        inputs, outputs = self.view.pins_for(row[0])
        pins = inputs | outputs
        from .sta import _scalar_pin
        for port, bit in pins:
            if scalar == _scalar_pin(port, bit, pins):
                return {"object": name, "instance": owner, "port": port, "bit": bit}
        return None

    def get(self, name, default=None):
        if name in self.cache:
            return self.cache[name]
        result = self._canonical(name)
        # Preserve the explicit escaped-provider spelling and ambiguity rule.
        if "\\\\" in name:
            unescaped = name.replace("\\\\", "\\")
            alias = self._canonical(unescaped)
            if alias is not None and unescaped.replace("\\", "\\\\") == name:
                if result is not None and result != alias:
                    raise ValidationError("STA provider object alias is ambiguous")
                result = alias
        self.cache[name] = result
        if len(self.cache) > 8192:
            self.cache.popitem(last=False)
        return default if result is None else result

    def __contains__(self, name):
        return self.get(name) is not None

    def __getitem__(self, name):
        result = self.get(name)
        if result is None:
            raise KeyError(name)
        return result


class _TimingTypes(Mapping):
    def __init__(self, view):
        self.view = view

    @staticmethod
    def for_instance(instance):
        cell_type = instance["type"]
        if cell_type in {"FDCE", "FDPE", "FDRE", "FDSE"} and _timing_parameter_is_one(instance.get("parameters", {}).get("IS_C_INVERTED")):
            return cell_type + "__NEG"
        return cell_type

    def __getitem__(self, key):
        from .emuir_compact import _decode
        row = self.view.connection.execute("""SELECT c.type,p.value FROM cell_records c
            LEFT JOIN payloads p ON p.seq=c.parameters_seq WHERE c.id=?""", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return self.for_instance({"type": row[0], "parameters": {} if row[1] is None else _decode(row[1], "parameters")})

    def __iter__(self):
        return (r[0] for r in self.view.connection.execute("SELECT id FROM cell_records ORDER BY seq"))

    def __len__(self):
        return self.view.counts["cell_records"]


class IndexedTimingView:
    def __init__(self, path: Path, *, include_ports=True):
        from .store_publication import bind_store_source, require_store_sources_unchanged
        self.source = bind_store_source(path, "emuir-compact-v2")
        self.connection = open_emuir_sqlite_read_only(path)
        try:
            if self.connection.execute("PRAGMA user_version").fetchone()[0] != 2:
                raise ValueError("indexed timing view requires compact EmuIR v2")
            self.connection.execute("PRAGMA cache_size=-65536")
            if include_ports:
                self.value = compact_metadata(self.connection)
            else:
                metadata = dict(self.connection.execute(
                    "SELECT key,value FROM metadata WHERE key IN ('logical_schema','design_json')"))
                self.value = {"schema": metadata["logical_schema"],
                              "design": _decode(metadata["design_json"], "design metadata"),
                              "ports": [], "warnings": [],
                              "clocks": [_decode(row[0], "clock") for row in self.connection.execute(
                                  "SELECT record_json FROM clocks ORDER BY seq")]}
            row = self.connection.execute("SELECT value FROM metadata WHERE key='record_counts_json'").fetchone()
            self.counts = _decode(row[0], "record counts") if row is not None else {}
            if set(self.counts) != {"cell_records", "net_records", "endpoint_records"} or any(
                isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in self.counts.values()
            ):
                raise ValueError("invalid compact EmuIR record counts")
            self.value["instances"] = _Rows(self, "cell_records", _timing_instances)
            self.value["nets"] = _Rows(self, "net_records", compact_nets)
            self.net_ids = _Ids(self, "net_records")
            self.instance_ids = _Ids(self, "cell_records")
            self.object_index = _Objects(self)
            require_store_sources_unchanged((self.source,))
        except BaseException:
            self.connection.close()
            raise

    def close(self):
        self.connection.close()

    def net_rows(self):
        count = 0
        for row in self.connection.execute("SELECT seq,id FROM net_records ORDER BY seq"):
            if row[0] != count:
                raise ValueError("non-contiguous compact EmuIR net ordinals")
            count += 1
            yield row
        if count != self.counts["net_records"]:
            raise ValueError("compact EmuIR net count mismatch")

    def pins_for(self, seq):
        inputs, outputs = set(), set()
        for row in self.connection.execute("""SELECT e.role,p.name,e.bit FROM endpoint_records e
            JOIN pin_names p ON p.seq=e.port_seq WHERE e.instance_seq=?""", (seq,)):
            (outputs if row[0] == 0 else inputs).add((row[1], row[2]))
        row = self.connection.execute("""SELECT p.value FROM cell_records c
            JOIN payloads p ON p.seq=c.constants_seq WHERE c.seq=?""", (seq,)).fetchone()
        from .emuir_compact import _decode
        for item in _decode(row[0], "constants"):
            inputs.add((item["port"], item["bit"]))
        return inputs, outputs

    def representative_graph(self):
        # Match exactly the first-occurrence specialization used by the old
        # Xilinx adapter; additionally retain a negative-clock representative.
        instances = [(row["seq"], _timing_instance(row))
                     for row in _timing_rows(self.connection, representatives=True)]
        nets = []
        for seq, instance in instances:
            inputs, outputs = self.pins_for(seq)
            for role, pins in (("sinks", inputs), ("drivers", outputs)):
                for port, bit in sorted(pins):
                    net = {"drivers": [], "sinks": []}
                    net[role] = [{"instance": instance["id"], "port": port, "bit": bit}]
                    nets.append(net)
        return SimpleNamespace(value={**self.value, "instances": [i for _, i in instances], "nets": nets})

    def timing_types(self):
        return _TimingTypes(self)

    def used_timing_types(self, timing_types):
        if timing_types is None:
            return [row[0] for row in self.connection.execute(
                "SELECT DISTINCT type FROM cell_records ORDER BY type")]
        if not isinstance(timing_types, _TimingTypes) or timing_types.view is not self:
            raise ValidationError("timing-type policy belongs to another source")
        return [row[0] for row in self.connection.execute(_NEGATIVE_TIMING_CELLS + """
            SELECT DISTINCT CASE WHEN n.seq IS NULL THEN c.type ELSE c.type||'__NEG' END AS timing_type
            FROM cell_records c LEFT JOIN negative_timing_cells n ON n.seq=c.seq
            ORDER BY timing_type""")]

    def timing_connections(self):
        # Integer-owner index order allows a cursor merge; no global pin map.
        cursor = iter(self.connection.execute("""SELECT e.instance_seq,e.net_seq,p.name AS port,e.bit,e.role
            FROM endpoint_records e INDEXED BY endpoint_owner
            LEFT JOIN pin_names p ON p.seq=e.port_seq WHERE e.instance_seq IS NOT NULL
            ORDER BY e.instance_seq,e.port_seq,e.bit"""))
        row = next(cursor, None)
        for seq, instance in enumerate(self.value["instances"]):
            wires = {}
            while row is not None and row["instance_seq"] < seq:
                raise ValueError("orphaned endpoint owner")
            while row is not None and row["instance_seq"] == seq:
                if row["role"] not in (0, 1) or row["port"] is None or row["bit"] < 0:
                    raise ValueError("invalid compact EmuIR endpoint")
                key = (row["port"], row["bit"])
                wires[key] = max(wires.get(key, -1), row["net_seq"])
                row = next(cursor, None)
            yield instance, wires
        if row is not None:
            raise ValueError("orphaned endpoint owner")
