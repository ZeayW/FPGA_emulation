"""Bind explicit small fixture pin facts to the current indexed export API.

No Bookshelf rendering or placement decisions live here. Raw JSON remains a
fixture source seal; the native exporter consumes the corresponding typed
pins, including deliberately invalid facts used by qualification tests.
"""
from contextlib import contextmanager, closing
import json
from pathlib import Path
import sqlite3
import tempfile

from emuflow_tools.physical.xilinx_netlist import open_xilinx_mapped_index, IndexedXilinxMappedNetlist
from emuflow_tools.physical.openparf import xilinx_openparf_atomic as atomic


@contextmanager
def mapped_facts(path, value=None):
    if isinstance(value, IndexedXilinxMappedNetlist):
        yield value
        return
    value = json.loads(Path(path).read_text()) if value is None else value
    modules = value["modules"]
    assert len(modules) == 1
    top, module = next(iter(modules.items()))
    assert len(module["cells"]) <= 4096
    with tempfile.TemporaryDirectory(prefix=".mapped-facts-", dir=Path(path).parent) as directory:
        index = Path(directory) / "mapped.sqlite"
        with closing(sqlite3.connect(index)) as db, db:
            db.executescript("""
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE cells(id TEXT PRIMARY KEY,seq INTEGER UNIQUE,type TEXT,record_json TEXT);
                CREATE TABLE cell_pins(cell_seq INTEGER,cell_id TEXT,port TEXT,port_bit INTEGER,
                    port_width INTEGER,direction INTEGER,value_kind INTEGER,net_bit INTEGER,
                    constant TEXT,PRIMARY KEY(cell_id,port,port_bit)) WITHOUT ROWID;
                CREATE TABLE netnames(id TEXT PRIMARY KEY,seq INTEGER,record_json TEXT);
            """)
            db.executemany("INSERT INTO metadata VALUES (?,?)", (
                ("schema", "emuflow.xilinx-mapped-index/v2"), ("top_json", json.dumps(top)),
                ("creator_json", '"explicit-fixture"'), ("ports_json", json.dumps(module.get("ports", {})))))
            for seq, (name, cell) in enumerate(module["cells"].items()):
                record = {"hide_name": 0, "parameters": {}, "attributes": {}, **cell}
                record.pop("connections", None); record.pop("port_directions", None)
                db.execute("INSERT INTO cells VALUES (?,?,?,?)", (name, seq, cell["type"], json.dumps(record)))
                for port, bits in cell.get("connections", {}).items():
                    direction = {"input": 0, "output": 1, "inout": 2}[cell["port_directions"][port]]
                    for bit, net in enumerate(bits):
                        integer = isinstance(net, int) and not isinstance(net, bool)
                        db.execute("INSERT INTO cell_pins VALUES (?,?,?,?,?,?,?,?,?)", (
                            seq, name, port, bit, len(bits), direction, 0 if integer else 1,
                            net if integer else None, None if integer else net))
        with open_xilinx_mapped_index(index) as owner:
            yield owner


def export_atomic_facts(*args, **kwargs):
    with mapped_facts(args[0], kwargs.get("mapped_value")) as owner:
        return atomic.export_xilinx_openparf_atomic(*args, **dict(kwargs, mapped_value=owner))
