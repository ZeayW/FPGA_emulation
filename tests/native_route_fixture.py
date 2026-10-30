"""Small typed routing inputs; routing and publication stay in the native stage."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile

from emuflow.common.errors import ValidationError
from emuflow_tools.document_io import read_json
from emuflow.partition.phase3_assignment_index import (
    ASSIGNMENT_INDEX_APPLICATION_ID, ASSIGNMENT_INDEX_SCHEMA,
    ASSIGNMENT_INDEX_USER_VERSION, CURRENT_DDL, CUT_RECORDS_SCHEMA,
    _json, _normalized_cut_record,
)
from emuflow_tools.stage_clients.routing_native import run_native_phase4
from tests.routing_validation_reference import static_exact_contract_from_assignment
from tests.timing_routing_reference import GLOBAL_CANDIDATE_PROVIDER
from tests.native_build import emuflow_native


def write_assignment_cuts(path, assignment, platform):
    """Persist only the current cut contract used by small routing cases."""
    cuts = assignment.get('cut_nets', [])
    if len(cuts) > 65536:
        raise ValidationError('routing fixture exceeds bounded cut population')
    parts = sorted(fpga.id for fpga in platform.fpgas)
    indices = {name: index for index, name in enumerate(parts)}
    contract = static_exact_contract_from_assignment(assignment)
    rows = [_normalized_cut_record({**cut, 'sink_fpgas': sorted(cut['sink_fpgas'])}, indices)
            for cut in sorted(cuts, key=lambda row: row['net'])]
    digest = hashlib.sha256()
    metadata = {'schema': ASSIGNMENT_INDEX_SCHEMA, 'design': assignment.get('design'),
                'platform': assignment.get('platform'), 'source_instance_count': 0,
                'semantic_contract_sha256': assignment.get('semantic_contract_sha256') if contract else None,
                'semantic_contract_schema': contract.get('schema') if contract else None,
                'cut_records_schema': CUT_RECORDS_SCHEMA, 'cut_records': len(rows)}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(f'PRAGMA application_id={ASSIGNMENT_INDEX_APPLICATION_ID};'
                         f'PRAGMA user_version={ASSIGNMENT_INDEX_USER_VERSION};'+CURRENT_DDL)
        db.executemany('INSERT INTO parts VALUES (?,?)', enumerate(parts))
        for index, row in enumerate(rows):
            digest.update(_json(row).encode()); digest.update(b'\n')
            source = indices[row['source_fpgas'][0]]
            db.execute('INSERT INTO cuts VALUES (?,?,?,?,?,?,?,?)',
                       (index,row['net'],source,row['cut_class'],row['sink_endpoints'],
                        row.get('transport_round'),row.get('dependency_level'),
                        row.get('combinational_dependency_depth')))
            db.executemany('INSERT INTO cut_sinks VALUES (?,?,?)',
                           ((row['net'],source,indices[sink]) for sink in row['sink_fpgas']))
            db.executemany('INSERT INTO cut_predecessors VALUES (?,?,?)',
                           ((index,ordinal,net) for ordinal,net in enumerate(row.get('predecessor_cut_nets',[]))))
        metadata['cut_records_sha256'] = digest.hexdigest()
        db.executemany('INSERT INTO metadata VALUES (?,?)', ((key,_json(value)) for key,value in metadata.items()))
    return path


def route_system_native(assignment, platform, constraints, timing_paths=None,
                        executable=None, provider=GLOBAL_CANDIDATE_PROVIDER,
                        candidate_workers=1):
    """Invoke the complete native owner and read its bounded test result."""
    if provider != GLOBAL_CANDIDATE_PROVIDER:
        raise ValueError(f'unsupported Phase 4 routing provider {provider!r}')
    with tempfile.TemporaryDirectory(prefix='native-route-fixture-') as temporary:
        root = Path(temporary)
        source = write_assignment_cuts(root/'assignment.sqlite', assignment, platform)
        board = root/'board.json'; board.write_text(json.dumps(platform.to_dict()))
        policy = root/'constraints.json'; policy.write_text(json.dumps(constraints))
        timing = None
        if timing_paths is not None:
            timing = root/'timing.json'; timing.write_text(json.dumps(timing_paths))
        output = root/'phase4'
        try:
            run_native_phase4(source, board, output, timing_paths=timing,
                              constraints_path=policy, candidate_workers=candidate_workers,
                              timing_driven=timing_paths is not None,
                              executable=str(emuflow_native()) if executable is None else executable)
        except Exception:
            assert not output.exists(), 'failed native route stage exposed a partial generation'
            raise
        return read_json(output/'routes.json')
