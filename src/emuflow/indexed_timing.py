"""Read-only streaming timing view of compact EmuIR, without a Python graph."""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

from .emuir_compact import _decode, compact_metadata, compact_nets
from .emuir_storage import open_emuir_sqlite_read_only
from .errors import ValidationError


def _timing_type_projection(clock_logic=False, lut_functions=False):
    joins = 'LEFT JOIN temp.timing_clock_cells t ON t.seq=c.seq' if clock_logic else ''
    expression = 't.timing_type' if clock_logic else 'NULL'
    if lut_functions:
        joins += ' LEFT JOIN source_lut_models.variants l ON l.source_type=c.type AND l.parameters_seq=c.parameters_seq'
        expression = f'coalesce({expression},l.timing_type)'
    return joins, expression


def _timing_instances(connection, clock_logic=False, lut_functions=False):
    """Do not decode resources, attributes or LUT/RAM INIT data for STA."""
    join, timing_type = _timing_type_projection(clock_logic, lut_functions)
    for row in connection.execute(f"""SELECT c.id,c.type,
        CASE WHEN c.type IN ('FDCE','FDPE','FDRE','FDSE') THEN p.value END AS parameters_json,
        {timing_type} AS timing_type,
        k.value AS constants_json FROM cell_records c
        LEFT JOIN payloads p ON p.seq=c.parameters_seq
        LEFT JOIN payloads k ON k.seq=c.constants_seq {join} ORDER BY c.seq"""):
        yield {"id": row["id"], "type": row["type"],
               "parameters": {} if row["parameters_json"] is None else _decode(row["parameters_json"], "parameters"),
               **({'_timing_type': row['timing_type']} if row['timing_type'] else {}),
               "constant_connections": _decode(row["constants_json"], "constants")}


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
        from .opensta import _integer_parameter
        cell_type = instance["type"]
        if instance.get('_timing_type'):
            return instance['_timing_type']
        if cell_type in {"FDCE", "FDPE", "FDRE", "FDSE"} and _integer_parameter(instance, "IS_C_INVERTED", 0) == 1:
            return cell_type + "__NEG"
        return cell_type

    def __getitem__(self, key):
        from .emuir_compact import _decode
        enabled = getattr(self.view, '_clock_logic_enabled', False)
        join, timing_type = _timing_type_projection(enabled, getattr(self.view, '_lut_functions_enabled', False))
        row = self.view.connection.execute(f"""SELECT c.type,p.value,{timing_type} FROM cell_records c
            LEFT JOIN payloads p ON p.seq=c.parameters_seq {join} WHERE c.id=?""", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return self.for_instance({"type": row[0], '_timing_type': row[2],
            "parameters": {} if row[1] is None else _decode(row[1], "parameters")})

    def __iter__(self):
        return (r[0] for r in self.view.connection.execute("SELECT id FROM cell_records ORDER BY seq"))

    def __len__(self):
        return self.view.counts["cell_records"]


class IndexedTimingView:
    def enable_source_lut_functions(self, model):
        import json
        import tempfile
        from .sta_lut_functions import build_source_lut_catalog, add_lut_catalog_models
        from .sqlite_artifacts import immutable_sqlite_uri
        from .store_publication import bind_store_source, require_store_sources_unchanged
        if getattr(self, '_lut_functions_enabled', False):
            raise ValidationError('source LUT functions already enabled')
        self._lut_scratch = tempfile.TemporaryDirectory(prefix='emuflow-source-lut-models-')
        output = Path(self._lut_scratch.name)/'models.sqlite'
        original = build_source_lut_catalog(self.source_path, output)
        catalog = bind_store_source(output, 'source-lut-model-store-v1')
        self._lut_bindings = (original, catalog)
        self.connection.execute('ATTACH DATABASE ? AS source_lut_models', (immutable_sqlite_uri(output),))
        header = self.connection.execute("SELECT value FROM source_lut_models.metadata WHERE key='header'").fetchone()
        header = json.loads(header[0]) if header else {}
        if (header.get('schema') != 'emuflow.source-lut-models/v1'
                or header.get('source_path') != str(original.path)
                or header.get('source_generation') != list(original.generation)):
            raise ValidationError('source LUT catalog belongs to another original generation')
        add_lut_catalog_models(self.connection, model)
        require_store_sources_unchanged(self._lut_bindings)
        self._lut_functions_enabled = True
        self.value['instances'] = _Rows(self, 'cell_records', lambda c: _timing_instances(
            c, getattr(self, '_clock_logic_enabled', False), True))

    def enable_clock_logic(self, model):
        if getattr(self, '_clock_logic_enabled', False):
            raise ValidationError('clock-cone projection already enabled')
        clocks = [(name, cell['clock']) for name, cell in model['cells'].items()
                  if cell['kind'] in {'rising_edge_ff', 'falling_edge_ff', 'rising_edge_bank'}]
        combinational = [name for name, cell in model['cells'].items() if cell['kind'] == 'combinational']
        if not clocks or not combinational:
            self.connection.execute('CREATE TEMP TABLE timing_clock_cells(seq INTEGER PRIMARY KEY,timing_type TEXT)')
        else:
            # UNION is the visited set: reconvergence/cycles never construct a
            # Python graph. The source connection remains read-only.
            self.connection.execute('CREATE TEMP TABLE timing_clock_cells(seq INTEGER PRIMARY KEY,timing_type TEXT)')
            self.connection.execute('''INSERT INTO temp.timing_clock_cells(seq)
                WITH RECURSIVE roots(type,pin) AS (VALUES ''' + ','.join('(?,?)' for _ in clocks) + '''),
                comb(type) AS (VALUES ''' + ','.join('(?)' for _ in combinational) + '''),
                cone(seq) AS (
                  SELECT DISTINCT d.instance_seq FROM cell_records c JOIN roots r ON r.type=c.type
                    JOIN endpoint_records e INDEXED BY endpoint_owner ON e.instance_seq=c.seq AND e.role=1
                    JOIN pin_names p ON p.seq=e.port_seq AND p.name=r.pin
                    JOIN endpoint_records d ON d.net_seq=e.net_seq AND d.role=0
                    JOIN cell_records driver ON driver.seq=d.instance_seq JOIN comb b ON b.type=driver.type
                  UNION
                  SELECT d.instance_seq FROM cone x
                    JOIN endpoint_records e INDEXED BY endpoint_owner ON e.instance_seq=x.seq AND e.role=1
                    JOIN endpoint_records d ON d.net_seq=e.net_seq AND d.role=0
                    JOIN cell_records driver ON driver.seq=d.instance_seq JOIN comb b ON b.type=driver.type
                ) SELECT seq FROM cone''', tuple(v for pair in clocks for v in pair) + tuple(combinational))
        self._clock_logic_enabled = True
        self.value['instances'] = _Rows(self, 'cell_records', lambda c: _timing_instances(c, True))

    def clock_logic_instances(self):
        for row in self.connection.execute('''SELECT c.id,c.type,p.value,c.seq,k.value FROM temp.timing_clock_cells t
            JOIN cell_records c ON c.seq=t.seq LEFT JOIN payloads p ON p.seq=c.parameters_seq
            JOIN payloads k ON k.seq=c.constants_seq ORDER BY c.seq'''):
            constants = _decode(row[4], 'constants')
            for pin in self.connection.execute('''SELECT p.name,e.bit,driver.type FROM endpoint_records e
                JOIN pin_names p ON p.seq=e.port_seq
                JOIN endpoint_records d ON d.net_seq=e.net_seq AND d.role=0
                JOIN cell_records driver ON driver.seq=d.instance_seq
                WHERE e.instance_seq=? AND e.role=1 AND driver.type IN ('GND','VCC')''', (row[3],)):
                constants.append({'port': pin[0], 'bit': pin[1], 'value': '0' if pin[2] == 'GND' else '1'})
            yield {'id': row[0], 'type': row[1], 'constant_connections': constants,
                   'parameters': {} if row[2] is None else _decode(row[2], 'parameters')}

    def bind_clock_logic_type(self, instance, timing_type):
        self.connection.execute('''UPDATE temp.timing_clock_cells SET timing_type=?
            WHERE seq=(SELECT seq FROM cell_records WHERE id=?)''', (timing_type, instance))

    def __init__(self, path: Path):
        self.source_path = Path(path).resolve()
        self.connection = open_emuir_sqlite_read_only(path)
        try:
            if self.connection.execute("PRAGMA user_version").fetchone()[0] != 2:
                raise ValueError("indexed timing view requires compact EmuIR v2")
            self.connection.execute("PRAGMA cache_size=-65536")
            self.value = compact_metadata(self.connection)
            from .emuir_compact import _decode
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
        except BaseException:
            self.connection.close()
            raise

    def close(self):
        try:
            if hasattr(self, '_lut_bindings'):
                from .store_publication import require_store_sources_unchanged
                require_store_sources_unchanged(self._lut_bindings)
        finally:
            self.connection.close()
            if hasattr(self, '_lut_scratch'):
                self._lut_scratch.cleanup()

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
        # Representatives own type discovery and negative-clock polarity.
        # Macro pin coverage comes separately from every source occurrence.
        instances, types, negative = [], set(), set()
        for seq, instance in enumerate(self.value["instances"]):
            cell_type = instance["type"]
            timing_type = _TimingTypes.for_instance(instance)
            if cell_type not in types or (timing_type != cell_type and cell_type not in negative):
                instances.append((seq, instance))
                types.add(cell_type)
                if timing_type != cell_type:
                    negative.add(cell_type)
        nets = []
        for seq, instance in instances:
            inputs, outputs = self.pins_for(seq)
            for role, pins in (("sinks", inputs), ("drivers", outputs)):
                for port, bit in sorted(pins):
                    net = {"drivers": [], "sinks": []}
                    net[role] = [{"instance": instance["id"], "port": port, "bit": bit}]
                    nets.append(net)
        return SimpleNamespace(value={**self.value, "instances": [i for _, i in instances], "nets": nets})

    def timing_type_pin_names(self, types):
        """Union emitted pin spellings over every occurrence of selected types.

        The model owns this bounded type/pin catalog. Read one macro at a time
        through the original owner index; no cell-to-pin graph is retained.
        Scalar and vector spellings must both survive when instances differ.
        """
        from .opensta import _scalar_pin_names
        if not types:
            return {}
        result = {}
        rows = self.connection.execute('SELECT seq,type FROM cell_records WHERE type IN ('+
            ','.join('?' for _ in types)+') ORDER BY seq', tuple(sorted(types)))
        for seq, cell_type in rows:
            inputs, outputs = self.pins_for(seq)
            pins = inputs | outputs
            entry = result.setdefault(cell_type, {'inputs': set(), 'outputs': set()})
            entry['inputs'].update(_scalar_pin_names(inputs, all_pins=pins))
            entry['outputs'].update(_scalar_pin_names(outputs, all_pins=pins))
            if entry['inputs'] & entry['outputs']:
                raise ValidationError('timing primitive pin changes direction across instances: '+cell_type)
        return result

    def timing_types(self):
        return _TimingTypes(self)

    def used_timing_types(self, timing_types):
        if timing_types is None:
            return sorted({r[0] for r in self.connection.execute("SELECT DISTINCT type FROM cell_records")})
        return sorted({timing_types.for_instance(i) for i in self.value["instances"]})

    def timing_connections(self, *, with_roles=False):
        # Integer-owner index order allows a cursor merge; no global pin map.
        cursor = iter(self.connection.execute("""SELECT e.instance_seq,e.net_seq,p.name AS port,e.bit,e.role
            FROM endpoint_records e INDEXED BY endpoint_owner
            LEFT JOIN pin_names p ON p.seq=e.port_seq WHERE e.instance_seq IS NOT NULL
            ORDER BY e.instance_seq,e.port_seq,e.bit"""))
        row = next(cursor, None)
        for seq, instance in enumerate(self.value["instances"]):
            wires = {}
            roles = {} if with_roles else None
            while row is not None and row["instance_seq"] < seq:
                raise ValueError("orphaned endpoint owner")
            while row is not None and row["instance_seq"] == seq:
                if row["role"] not in (0, 1) or row["port"] is None or row["bit"] < 0:
                    raise ValueError("invalid compact EmuIR endpoint")
                key = (row["port"], row["bit"])
                if roles is not None:
                    if key in roles and roles[key] != row['role']:
                        raise ValidationError('timing primitive pin has conflicting directions')
                    roles[key] = row['role']
                wires[key] = max(wires.get(key, -1), row["net_seq"])
                row = next(cursor, None)
            yield (instance, wires, roles) if with_roles else (instance, wires)
        if row is not None:
            raise ValueError("orphaned endpoint owner")

    def _validate_provider_name_aliases(self):
        """Check only possible escaped-name collisions, not every emitted pin.

        IDs and pin rows have canonical indexed owners. A doubled owner can
        collide only with an existing literal doubled ID; the indexed join
        finds those pairs without repeated Python endpoint reconstruction.
        """
        if getattr(self, "_provider_aliases_validated", False):
            return
        from .sta import _scalar_pin
        pairs = self.connection.execute("""SELECT a.seq,a.id,b.seq,b.id
            FROM cell_records a JOIN cell_records b ON b.id=replace(a.id,?,?)
            WHERE instr(a.id,?)>0 AND a.id<>b.id""", ("\\", "\\\\", "\\"))
        for seq_a, owner_a, seq_b, owner_b in pairs:
            ia, oa = self.pins_for(seq_a)
            ib, ob = self.pins_for(seq_b)
            pins_a, pins_b = ia | oa, ib | ob
            aliases_a = {(owner_a + "/" + _scalar_pin(p, bit, pins_a)).replace("\\", "\\\\")
                         for p, bit in pins_a}
            names_b = {owner_b + "/" + _scalar_pin(p, bit, pins_b) for p, bit in pins_b}
            if aliases_a & names_b:
                raise ValidationError("STA provider object alias is ambiguous")
        # Top-level names are bounded metadata. They can also alias cell pins.
        for port in self.value["ports"]:
            if "\\" not in port["id"]:
                continue
            for bit in range(port["width"]):
                names = [port["id"]] if port["width"] == 1 else [f"{port['id']}[{bit}]", f"{port['id']}__{bit}"]
                for name in names:
                    self.object_index.get(name)
                    self.object_index.get(name.replace("\\", "\\\\"))
        self._provider_aliases_validated = True

    def timing_verilog_lines(self, timing_cell_types, timing_model_pins=None):
        from .verilog import _identifier, _timing_pin_name
        self._validate_provider_name_aliases()
        ports = self.value["ports"]
        widths = {p["id"]: p["width"] for p in ports}
        yield f"module {_identifier(self.value['design']['top'])}("
        yield "  " + ",\n  ".join(_identifier(p["id"]) for p in ports)
        yield ");"
        for port in ports:
            if port["direction"] not in {"input", "output", "inout"}:
                raise ValidationError("cannot emit unknown-direction port")
            width = "" if port["width"] == 1 else f"[{port['width'] - 1}:0] "
            yield f"  {port['direction']} {width}{_identifier(port['id'])};"
        yield ""
        for row in self.net_rows():
            yield f"  wire __emuflow_net_{row[0]};"
        yield ""
        for row in self.connection.execute("""SELECT e.net_seq,e.role,p.name,e.bit
            FROM endpoint_records e LEFT JOIN pin_names p ON p.seq=e.port_seq
            WHERE e.instance_seq IS NULL ORDER BY e.net_seq,e.role,e.ordinal"""):
            if row[1] not in (0, 1) or row[2] not in widths or row[3] < 0 or row[3] >= widths[row[2]]:
                raise ValueError("invalid compact EmuIR top-level endpoint")
            select = "" if widths[row[2]] == 1 else f"[{row[3]}]"
            port = _identifier(row[2]) + select
            wire = f"__emuflow_net_{row[0]}"
            yield f"  assign {wire} = {port};" if row[1] == 0 else f"  assign {port} = {wire};"
        for port in ports:
            for constant in port.get("constant_connections", []):
                select = "" if port["width"] == 1 else f"[{constant['bit']}]"
                yield f"  assign {_identifier(port['id'])}{select} = 1'b{constant['value']};"
        yield ""
        for instance, wires, roles in self.timing_connections(with_roles=True):
            constants = {(p["port"], p["bit"]): p["value"] for p in instance.get("constant_connections", [])}
            pins = set(wires) | set(constants)
            connections = []
            cell_type = (timing_cell_types.for_instance(instance) if isinstance(timing_cell_types, _TimingTypes)
                         else timing_cell_types.get(instance['id'], instance['type']) if timing_cell_types is not None
                         else instance['type'])
            if timing_model_pins is not None:
                from .verilog import require_timing_model_pin
                inputs, outputs = set(constants), set()
                # Only the current cell's endpoint roles are needed. The
                # cursor merge exposes them without another owner-index read.
                for port_bit, role in roles.items():
                    (outputs if role == 0 else inputs).add(port_bit)
                for role, collection in ((1, inputs), (0, outputs)):
                    for port, bit in collection:
                        require_timing_model_pin(timing_model_pins, cell_type,
                            _timing_pin_name(pins, port, bit), role, instance['id'])
            if any("\\" in port for port, _ in pins):
                names = {instance["id"] + "/" + _timing_pin_name(pins, p, bit) for p, bit in pins}
                for name in names:
                    alias = name.replace("\\", "\\\\")
                    if alias != name and alias in names:
                        raise ValidationError("STA provider object alias is ambiguous")
            for port, bit in sorted(pins):
                expression = f"__emuflow_net_{wires[(port, bit)]}" if (port, bit) in wires else f"1'b{constants[(port, bit)]}"
                connections.append(f".{_identifier(_timing_pin_name(pins, port, bit))}({expression})")
            yield f"  {_identifier(cell_type)} {_identifier(instance['id'])}("
            yield "    " + ",\n    ".join(connections)
            yield "  );"
        yield "endmodule"
        yield ""
