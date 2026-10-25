"""Bounded random-access view used by physical timing query generation.

The physical backends only need identities, ordinals, cell types and a small
number of endpoint pin inventories.  Reconstructing the full EmuIR graph for
those lookups multiplied a compact SQLite artifact into tens of GiB of Python
objects.  This view keeps SQLite as the data plane and caches only requested
records.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Iterator

from .emuir_compact import _decode, compact_metadata, selected_net_records
from .emuir_storage import open_emuir_sqlite_read_only
from .store_publication import bind_store_source, require_store_sources_unchanged
from .errors import ValidationError


class _BoundedMapping(Mapping):
    def __init__(self, view: "IndexedPhysicalIR", kind: str):
        self.view = view
        self.kind = kind
        self.cache: OrderedDict[str, Any] = OrderedDict()

    def _lookup(self, key: str) -> Any:
        c = self.view.connection
        if self.kind == "instance_index":
            row = c.execute(
                "SELECT seq FROM cell_records WHERE id=?", (key,)
            ).fetchone()
            return None if row is None else int(row[0])
        if self.kind == "net_index":
            row = c.execute(
                "SELECT seq FROM net_records WHERE id=?", (key,)
            ).fetchone()
            return None if row is None else int(row[0])
        if self.kind == "instance":
            row = c.execute(
                """SELECT cell.*,resources.value AS resources_json,
                parameters.value AS parameters_json,
                attributes.value AS attributes_json,
                constants.value AS constants_json,extras.value AS extras_json
                FROM cell_records cell
                JOIN payloads resources ON resources.seq=cell.resources_seq
                LEFT JOIN payloads parameters ON parameters.seq=cell.parameters_seq
                LEFT JOIN payloads attributes ON attributes.seq=cell.attributes_seq
                JOIN payloads constants ON constants.seq=cell.constants_seq
                JOIN payloads extras ON extras.seq=cell.extras_seq
                WHERE cell.id=?""",
                (key,),
            ).fetchone()
            if row is None:
                return None
            record = {
                "id": row["id"],
                "type": row["type"],
                "resources": _decode(row["resources_json"], "resources"),
                "constant_connections": _decode(
                    row["constants_json"], "constants"
                ),
                **_decode(row["extras_json"], "instance extras"),
            }
            if row["parameters_json"] is not None:
                record["parameters"] = _decode(
                    row["parameters_json"], "parameters"
                )
            if row["attributes_json"] is not None:
                record["attributes"] = _decode(
                    row["attributes_json"], "attributes"
                )
            record["name"] = (
                row["id"] if row["flags"] & 1 else row["name"]
            )
            return record
        if self.kind == "pins":
            row = c.execute(
                "SELECT seq,constants_seq FROM cell_records WHERE id=?", (key,)
            ).fetchone()
            if row is None:
                return None
            pins = {
                (item[0], int(item[1]))
                for item in c.execute(
                    """SELECT p.name,e.bit FROM endpoint_records e
                    JOIN pin_names p ON p.seq=e.port_seq
                    WHERE e.instance_seq=?""",
                    (row["seq"],),
                )
            }
            constants = c.execute(
                "SELECT value FROM payloads WHERE seq=?", (row["constants_seq"],)
            ).fetchone()
            for endpoint in _decode(constants[0], "constants"):
                pins.add((endpoint["port"], endpoint["bit"]))
            return pins
        if self.kind == "net":
            # Membership probes are routine while translating an original
            # timing path into one partition: most original nets are absent
            # from any particular split netlist.  ``selected_net_records`` is
            # deliberately strict for required batches, so first distinguish
            # an ordinary miss from a corrupt/missing required record.  The
            # old implementation let its ValueError escape from ``__contains__``
            # and made every partition eventually fail on a net owned by a
            # different FPGA.
            present = c.execute(
                "SELECT 1 FROM net_records WHERE id=?", (key,)
            ).fetchone()
            if present is None:
                return None
            records = selected_net_records(c, [key])
            return records.get(key)
        raise AssertionError(self.kind)

    def __getitem__(self, key: str) -> Any:
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        value = self._lookup(key)
        if value is None:
            raise KeyError(key)
        self.cache[key] = value
        if len(self.cache) > 8192:
            self.cache.popitem(last=False)
        return value

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        try:
            self[key]
        except KeyError:
            return False
        return True

    def __iter__(self) -> Iterator[str]:
        table = (
            "net_records"
            if self.kind in {"net", "net_index"}
            else "cell_records"
        )
        return (
            row[0]
            for row in self.view.connection.execute(
                f"SELECT id FROM {table} ORDER BY seq"
            )
        )

    def __len__(self) -> int:
        table = (
            "net_records"
            if self.kind in {"net", "net_index"}
            else "cell_records"
        )
        return int(
            self.view.connection.execute(
                f"SELECT count(*) FROM {table}"
            ).fetchone()[0]
        )


class IndexedPhysicalIR:
    """Read only the physical identities requested by one backend worker."""

    def __init__(self, path: Path):
        self.store_path = Path(path).resolve()
        self._closed = False
        self._source_bindings = (
            bind_store_source(self.store_path, "emuir-compact-v2"),
        )
        self.connection = open_emuir_sqlite_read_only(path)
        try:
            if self.connection.execute("PRAGMA user_version").fetchone()[0] != 2:
                raise ValueError("indexed physical view requires compact EmuIR v2")
            self.connection.execute("PRAGMA cache_size=-65536")
            self.value = compact_metadata(self.connection)
            self.instances = _BoundedMapping(self, "instance")
            self.nets = _BoundedMapping(self, "net")
            self.instance_indices = _BoundedMapping(self, "instance_index")
            self.net_indices = _BoundedMapping(self, "net_index")
            self.pins = _BoundedMapping(self, "pins")
            require_store_sources_unchanged(self._source_bindings)
        except BaseException:
            self.connection.close()
            raise

    def bound_store_sources(self):
        if self._closed:
            raise ValidationError("indexed physical view is closed")
        require_store_sources_unchanged(self._source_bindings)
        return self._source_bindings

    def close(self) -> None:
        self._closed = True
        self.connection.close()

    def __enter__(self) -> "IndexedPhysicalIR":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def top_binding(self, port: str, bit: int) -> tuple[int, int]:
        rows = list(
            self.connection.execute(
                """SELECT e.net_seq,e.role FROM endpoint_records e
                JOIN pin_names p ON p.seq=e.port_seq
                WHERE e.instance_seq IS NULL AND p.name=? AND e.bit=?""",
                (port, bit),
            )
        )
        if len(rows) != 1 or rows[0][1] not in (0, 1):
            raise KeyError((port, bit))
        return int(rows[0][0]), int(rows[0][1])
