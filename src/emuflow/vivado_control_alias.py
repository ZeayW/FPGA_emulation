"""Bounded structural FF association through duplicated LUT1 control drivers.

Clock and D net identities remain exact. Only identical, fully specified LUT1
drivers of CE/reset/set controls can replace their output-net identity. This
is not general Boolean equivalence, retiming, or a sequential deletion proof.
"""
from __future__ import annotations

from functools import lru_cache
import json

from .errors import ValidationError


class ControlAliasLookup:
    def __init__(self, connection, stage):
        self.connection, self.stage = connection, stage
        self.pins = lru_cache(maxsize=4096)(self._pins)
        self.control = lru_cache(maxsize=4096)(self._control)
        self.signature = lru_cache(maxsize=4096)(self._signature)

    def close(self):
        self.pins.cache_clear()
        self.control.cache_clear()
        self.signature.cache_clear()

    def _pins(self, origin):
        return tuple(self.connection.execute(
            "SELECT port,port_bit,port_width,direction,value_kind,net_bit,constant "
            "FROM mapped.cell_pins WHERE cell_seq=? ORDER BY port,port_bit", (origin,)))

    def _control(self, kind, net, constant):
        raw = (kind, net, constant)
        if kind != 0:
            return raw
        drivers = tuple(self.connection.execute(
            "SELECT p.cell_seq,c.type,c.record_json FROM mapped.cell_pins p "
            "INDEXED BY cell_pins_by_net JOIN mapped.cells c ON c.seq=p.cell_seq "
            "WHERE p.value_kind=0 AND p.net_bit=? AND p.direction=1 LIMIT 2", (net,)))
        if len(drivers) != 1 or drivers[0][1] != "LUT1":
            return raw
        seq, _, record = drivers[0]
        parameters = json.loads(record).get("parameters", {})
        init = parameters.get("INIT")
        if not isinstance(init, str) or len(init) != 2 or set(init) - {"0", "1"}:
            return raw
        pins = self.pins(seq)
        if (len(pins) != 2 or pins[0][:4] != ("I0", 0, 1, 0)
                or pins[1][:4] != ("O", 0, 1, 1)
                or pins[1][4:] != (0, net, None)
                or (pins[0][4] == 1 and pins[0][6] not in {"0", "1"})):
            return raw
        return ("LUT1", json.dumps(parameters, sort_keys=True, separators=(",", ":")), pins[0][4:])

    def _signature(self, origin):
        row = self.connection.execute(
            "SELECT type,record_json FROM mapped.cells WHERE seq=?", (origin,)).fetchone()
        if row is None or row[0] not in {"FDRE", "FDSE", "FDCE", "FDPE"}:
            return None
        pins = []
        for port, bit, width, direction, kind, net, constant in self.pins(origin):
            layout = (port, bit, width, direction)
            if direction == 1:
                value = ()
            elif kind == 1 and constant not in {"0", "1"}:
                return None
            elif port in {"CE", "R", "S", "CLR", "PRE"}:
                value = self.control(kind, net, constant)
            else:
                value = (kind, net, constant)
            pins.append((layout, value))
        parameters = json.loads(row[1]).get("parameters", {})
        return row[0], json.dumps(parameters, sort_keys=True, separators=(",", ":")), tuple(pins)

    def equivalent_origins(self, origin, reference):
        if reference not in {"FDRE", "FDSE", "FDCE", "FDPE"}:
            return ()
        source = self.signature(origin)
        pins = self.pins(origin)
        clock = next((p for p in pins if p[:4] == ("C", 0, 1, 0)), None)
        data = next((p for p in pins if p[:4] == ("D", 0, 1, 0)), None)
        if source is None or clock is None or data is None or clock[4] != 0 or data[4] != 0:
            return ()
        # Bound the source-net fanout before any cell or stage association.
        # A limit after filtering matching objects could still scan a whole
        # high-fanout graph, even though its returned class was small.
        sinks = tuple(self.connection.execute(
            "SELECT cell_seq,port,port_bit,port_width FROM mapped.cell_pins "
            "INDEXED BY cell_pins_by_net WHERE value_kind=0 AND net_bit=? "
            "AND direction=0 LIMIT 4097", (data[5],)))
        if len(sinks) > 4096:
            raise ValidationError("Vivado structural control source-net fanout exceeds bounded lookup scope")
        result = []
        for candidate, port, bit, width in sinks:
            if (port, bit, width) != ("D", 0, 1):
                continue
            retained = self.connection.execute(
                "SELECT 1 FROM objects INDEXED BY objects_origin "
                "WHERE stage=? AND origin=? AND ref=? LIMIT 1",
                (self.stage, candidate, reference)).fetchone()
            if retained is not None and self.signature(candidate) == source:
                result.append(candidate)
        return tuple(sorted(result))
