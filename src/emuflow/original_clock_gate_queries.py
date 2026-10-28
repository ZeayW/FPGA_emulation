"""Bounded native observation requests for original clock-gating checks.

The complete original index remains the member/clock-budget owner. These
requests follow the *local mapped input driver*, including RX shadows, rather
than requiring the original gating LUT or a remote enable FF to survive.
Native fanout observations are candidate check collections, not equivalence
proofs, elimination certificates or numerical timing results.
"""
from contextlib import ExitStack, closing
import csv
import json
from pathlib import Path
import sqlite3
import tempfile

from .errors import ValidationError
from .store_publication import StoreSource, bind_store_source, require_store_sources_unchanged

HEADER = ('endpoint_seq', 'gate_origin', 'gate_pin_hex', 'driver_origin', 'driver_pin_hex')


def write_original_clock_gate_queries(endpoint_index, source, mapped, output, *, max_endpoints=4096):
    """Write small per-partition requests without materializing the circuit.

    Every non-state capture endpoint receives a catalog entry, including
    unsupported shapes and missing mapped copies. Only scalar AND-gate enable
    inputs with a unique local mapped driver receive fanout requests. A missing
    native object remains an observation to resolve, never an automatic error
    that stops physical implementation or a zero-delay path.
    """
    index_path, source = Path(endpoint_index).resolve(), Path(source).resolve()
    mapped = {name: Path(path).resolve() for name, path in mapped.items()}
    if type(max_endpoints) is not int or not 0 < max_endpoints <= 65536:
        raise ValidationError('original clock-gate request bound is invalid')
    output = Path(output).resolve()
    if output.exists():
        raise ValidationError('original clock-gate request destination already exists')
    index_binding = bind_store_source(index_path, 'native-original-endpoint-index-v1')
    with ExitStack() as stack:
        def connect(path):
            return stack.enter_context(closing(sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1', uri=True)))
        index = connect(index_path)
        metadata = dict(index.execute('SELECT key,value FROM metadata'))
        if metadata.get('schema') != 'emuflow.native-original-endpoint-index/v1':
            raise ValidationError('original clock-gate source index schema differs')
        parents = tuple(StoreSource(Path(p['path']), p['schema'], tuple(p['generation']))
                        for p in json.loads(metadata['source_generations']))
        require_store_sources_unchanged((index_binding, *parents))
        by_path = {parent.path: parent for parent in parents}
        if source not in by_path or by_path[source].schema != 'emuir-compact-v2':
            raise ValidationError('original clock-gate source belongs to another endpoint index')
        parts = list(index.execute('SELECT part_index,fpga FROM parts ORDER BY part_index'))
        if set(mapped) != {name for _, name in parts}:
            raise ValidationError('original clock-gate mapped partition coverage differs')
        # The original index preserves ordered mapped parents. A caller cannot
        # exchange same-schema stores between FPGA names.
        mapped_parents = [p for p in parents if p.schema == 'xilinx-mapped-index-v2']
        if [p.path for p in mapped_parents] != [mapped[name] for _, name in parts]:
            raise ValidationError('original clock-gate mapped partition owners differ')
        source_db = connect(source)
        stores = {part: connect(mapped[name]) for part, name in parts}
        counts = index.execute("SELECT count(*) FROM endpoints WHERE role=1 AND source_kind='cell-input'").fetchone()[0]
        if counts > max_endpoints:
            raise ValidationError('original clock-gate endpoint population exceeds its explicit request bound')
        catalog, requests = [], {name: [] for _, name in parts}
        names = dict(parts)
        for endpoint, cell, port, bit in index.execute(
                "SELECT seq,source_cell_seq,source_port_seq,bit FROM endpoints "
                "WHERE role=1 AND source_kind='cell-input' ORDER BY seq"):
            port_name = source_db.execute('SELECT name FROM pin_names WHERE seq=?', (port,)).fetchone()
            if port_name is None:
                raise ValidationError('original clock-gate source pin coordinate is absent')
            owners = list(index.execute('SELECT part_index,mapped_cell_seq FROM cell_owners WHERE source_cell_seq=? ORDER BY part_index,seq', (cell,)))
            if not owners:
                catalog.append(dict(endpoint=endpoint, source_cell=cell, status='missing-partition-owner'))
            for part, origin in owners:
                entry = dict(endpoint=endpoint, source_cell=cell, port=port_name[0], bit=bit,
                             fpga=names[part], mapped_origin=origin, status='unsupported-source-shape')
                catalog.append(entry)
                if origin is None:
                    entry['status'] = 'missing-mapped-copy'; continue
                db = stores[part]
                gate = db.execute('SELECT id,type,record_json FROM cells WHERE seq=?', (origin,)).fetchone()
                if gate is None:
                    raise ValidationError('original clock-gate mapped owner is absent')
                identity, reference, record = gate
                entry['mapped_type'] = reference
                if (reference, port_name[0], bit) != ('LUT2', 'I1', 0) or json.loads(record).get('parameters', {}).get('INIT') != '1000':
                    continue
                enable = db.execute("SELECT direction,value_kind,net_bit FROM cell_pins WHERE cell_id=? AND port='I1' AND port_bit=0", (identity,)).fetchone()
                if enable is None or enable[:2] != (0, 0):
                    entry['status'] = 'non-net-enable'; continue
                drivers = list(db.execute('SELECT cell_seq,port,port_bit,port_width FROM cell_pins WHERE value_kind=0 AND net_bit=? AND direction=1', (enable[2],)))
                if len(drivers) != 1:
                    entry['status'] = 'non-unique-local-enable-driver'; continue
                driver, driver_port, driver_bit, width = drivers[0]
                pin = driver_port if width == 1 else f'{driver_port}[{driver_bit}]'
                entry.update(status='local-driver-query', driver_origin=driver, driver_pin=pin,
                             enable_net_bit=enable[2])
                requests[names[part]].append((endpoint, f's{origin}', b'I1'.hex(), f's{driver}', pin.encode().hex()))
        require_store_sources_unchanged((index_binding, *parents))
        # Publish the complete bounded request set together; never leave a
        # partial directory that a read-only observer could mistake for ready.
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.original-gate-queries-', dir=output.parent) as temporary:
            root = Path(temporary)/'requests'; root.mkdir()
            for name, rows in requests.items():
                with (root/(name+'.tsv')).open('w', newline='') as stream:
                    writer = csv.writer(stream, delimiter='\t', lineterminator='\n')
                    writer.writerow(HEADER); writer.writerows(rows)
            result = dict(schema='emuflow.original-clock-gate-requests/v1', endpoints=counts,
                          queries=sum(map(len, requests.values())), catalog=catalog,
                          source_generations=[dict(path=str(p.path), schema=p.schema, generation=p.generation)
                                              for p in (index_binding, *parents)],
                          physical_acceptance='pending', original_path_sta_acceptance='pending',
                          scope='Local mapped driver and native fanout requests only; missing objects are not elimination proofs')
            (root/'catalog.json').write_text(json.dumps(result, sort_keys=True, indent=2)+'\n')
            require_store_sources_unchanged((index_binding, *parents))
            root.rename(output)
    return result
