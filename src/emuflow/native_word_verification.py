"""Bound the size of independent OpenSTA witness models without sampling.

Each event is an independent chain with its own fixed launch and deadline.
Splitting those chains changes neither their timing graph nor their constraints.
The merged output retains every original event in its original order.
"""
from pathlib import Path
import hashlib
import json

from .errors import ValidationError
from .io import file_sha256, write_json


def run_native_event_checks(checks, directory, executable=None, *, maximum_checks=8192):
    from .global_sta import (
        validate_checks, run_event_checks, read_measurements, read_engine_identity,
    )
    if type(maximum_checks) is not int or maximum_checks <= 0:
        raise ValidationError('native witness engine bound must be a positive integer')
    rows = validate_checks(checks, require_observation_pairs=False)
    directory = Path(directory)
    if len(rows) <= maximum_checks:
        return run_event_checks(rows, directory, executable, require_observation_pairs=False)

    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / '.measurements.batched.tmp'
    batches = []
    identity = None
    try:
        with temporary.open('w') as merged, (directory / 'opensta.log').open('w') as log:
            merged.write('endpoint\tarrival_ns\trequired_ns\tslack_ns\n')
            for number, begin in enumerate(range(0, len(rows), maximum_checks)):
                selected = rows[begin:begin + maximum_checks]
                case = directory / f'batch{number:04d}'
                values = run_event_checks(selected, case, executable, require_observation_pairs=False)
                if len(values) != len(selected):
                    raise ValidationError('native witness batch omitted an event')
                engine = read_engine_identity(case / 'opensta.log')
                if identity is None:
                    identity = engine
                if engine != identity:
                    raise ValidationError('native witness batches use different engines')
                count = 0
                with (case / 'measurements.tsv').open() as stream:
                    if stream.readline() != 'endpoint\tarrival_ns\trequired_ns\tslack_ns\n':
                        raise ValidationError('native witness batch measurement header differs')
                    for index, line in enumerate(stream):
                        fields = line.rstrip('\n').split('\t')
                        if len(fields) != 4 or fields[0] != f'o{index}' or index >= len(selected):
                            raise ValidationError('native witness batch measurement order differs')
                        merged.write(f'o{begin + index}\t' + '\t'.join(fields[1:]) + '\n')
                        count += 1
                if count != len(selected):
                    raise ValidationError('native witness batch measurement population differs')
                log.write((case / 'opensta.log').read_text())
                log.write('\n')
                log.flush()
                digest = hashlib.sha256()
                for row in selected:
                    digest.update((json.dumps((row.path, row.role, row.event, row.launch_ns,
                                               row.arcs_ns, row.required_ns),
                                              separators=(',', ':'), allow_nan=False) + '\n').encode())
                batches.append({
                    'begin_check': begin, 'end_check': begin + count,
                    'check_identity_sha256': digest.hexdigest(),
                    'artifacts': {name: {'artifact': f'{case.name}/{name}',
                                         'sha256': file_sha256(case / name)}
                                  for name in ('global_timing.v', 'global_timing.lib', 'global_timing.sdc',
                                               'analyze.tcl', 'measurements.tsv', 'opensta.log')},
                })
        # Validate against the complete original sequence before publication.
        values = read_measurements(temporary, rows)
        temporary.replace(directory / 'measurements.tsv')
        write_json(directory / 'native-word-batch-manifest.json', {
            'schema': 'emuflow.native-word-witness-batches/v1', 'status': 'complete',
            'checks': len(rows), 'maximum_checks_per_engine': maximum_checks,
            'engine': identity, 'batches': batches,
            'scope': 'Every independent event chain retained in original order; no sampling',
        })
        return values
    finally:
        temporary.unlink(missing_ok=True)
