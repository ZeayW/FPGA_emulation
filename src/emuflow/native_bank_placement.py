"""Bind canonical transport roles to the physical banks used for placement."""
from contextlib import closing
import csv
import json
from pathlib import Path
import re
import sqlite3
import tempfile

from .errors import ValidationError
from .io import file_sha256, write_json
from .lvds_word_plan import WORD_PHY_PLAN_SCHEMA
from .native_word_source_catalog import SCHEMA as SOURCE_SCHEMA
from .store_publication import bind_store_source, require_store_sources_unchanged
from .tdm_word import WORD_EVENT_POLICY

POLICY = 'native-bank-slr-affinity-and-clock-region-control-replication-v1'


def _require(condition, message):
    if not condition:
        raise ValidationError(message)


def read_native_bank_placement_request(path, fpga):
    """Read an immutable request, including retained producer ownership."""
    path = Path(path).resolve()
    document = json.loads(path.read_text())
    _require(document.get('schema') == 'emuflow.native-bank-placement-request/v1'
             and document.get('status') == 'complete' and document.get('fpga') == fpga
             and document.get('policy') == POLICY,
             'native bank placement request owner or policy differs')
    _require(set(document.get('files', {})) == {'banks.tsv', 'rx-bank-roles.tsv'},
             'native bank placement request tables differ')
    references = {**document['input_sha256'],
                  **{value['path']: value['sha256'] for value in document['files'].values()}}
    parents = [bind_store_source(path, 'physical-input-document-v1')]
    for name, digest in references.items():
        parent = bind_store_source(Path(name), 'physical-input-document-v1')
        _require(file_sha256(parent.path) == digest, 'native bank placement request source changed')
        parents.append(parent)
    for name, reference in document['files'].items():
        _require(Path(reference['path']).resolve() == Path(document['directory']) / name,
                 'native bank placement table directory differs')
    require_store_sources_unchanged(parents)
    return document, parents


def write_native_bank_placement_request(fpga, plan_path, source_directory, output):
    """Emit transport-only placement inputs without visiting the DUT graph.

    Native optimized projections are looked up later in Vivado. An absent
    projection remains a reported fact; it is never protected with DONT_TOUCH.
    """
    plan_path = Path(plan_path).resolve()
    source_directory = Path(source_directory).resolve()
    output = Path(output).resolve()
    descriptor = source_directory / 'source-catalog.json'
    catalog = source_directory / 'catalog.sqlite'
    roles = source_directory / 'rx-roles.tsv'
    parents = [bind_store_source(path, 'physical-input-document-v1')
               for path in (plan_path, descriptor, catalog, roles)]
    plan = json.loads(plan_path.read_text())
    owner = json.loads(descriptor.read_text())
    _require(plan.get('schema') == WORD_PHY_PLAN_SCHEMA and plan.get('status') == 'pass'
             and plan.get('word_event_policy') == WORD_EVENT_POLICY,
             'native bank placement requires a checked eight-bit word plan')
    _require(owner.get('schema') == 'emuflow.native-word-source-preparation/v1'
             and owner.get('status') == 'complete' and owner.get('fpga') == fpga
             and owner.get('directory') == str(source_directory),
             'native bank placement source owner differs')
    for path in (catalog, roles):
        _require(file_sha256(path) == owner['files'][path.name]['sha256'],
                 'native bank placement source changed')
    partitions = [record for record in plan['fpgas'] if record['fpga'] == fpga]
    _require(len(partitions) == 1, 'native bank placement partition is absent or repeated')
    banks = []
    seen = set()
    for bank in partitions[0]['banks']:
        raw = bank['bank']
        number = str(raw)
        _require(type(raw) in (str, int) and re.fullmatch(r'[0-9]+', number) and number not in seen
                 and bank['direction'] in ('rx', 'tx')
                 and type(bank['logical_lsb']) is int and bank['logical_lsb'] >= 0
                 and bank['logical_lsb'] % 160 == 0,
                 'native bank placement has an invalid bank or word slice')
        seen.add(number)
        banks.append({**bank, 'bank': number})
    receive = []
    with closing(sqlite3.connect(catalog.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        metadata = {key: json.loads(value) for key, value in db.execute('SELECT * FROM metadata')}
        _require(metadata.get('schema') == SOURCE_SCHEMA and metadata.get('fpga') == fpga,
                 'native bank placement catalog belongs to another partition')
        for origin, link, peer, sink, lane in db.execute(
                "SELECT p.source_origin,e.link,e.source,e.sink,e.lane FROM endpoints p "
                "JOIN entries e ON e.id=p.entry_id WHERE p.kind='rx' ORDER BY p.source_origin,p.id"):
            matches = [bank for bank in banks if bank['direction'] == 'rx'
                       and bank['link'] == link and bank['peer'] == peer
                       and bank['logical_lsb'] <= lane < bank['logical_lsb'] + 160]
            _require(sink == fpga and len(matches) == 1,
                     'native RX role has no unique physical bank slice')
            receive.append((origin, matches[0]['bank']))
    _require(not output.exists(), 'native bank placement destination already exists')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.native-bank-placement-', dir=output.parent) as temporary:
        staging = Path(temporary) / 'request'
        staging.mkdir()
        for name, header, rows in (
                ('banks.tsv', ('bank', 'direction', 'instance'),
                 ((b['bank'], b['direction'], f"native_bank{b['bank']}_{b['direction']}")
                  for b in banks)),
                ('rx-bank-roles.tsv', ('source_origin', 'bank'), receive)):
            with (staging / name).open('w', newline='') as stream:
                writer = csv.writer(stream, delimiter='\t', lineterminator='\n')
                writer.writerow(header)
                writer.writerows(rows)
        result = dict(schema='emuflow.native-bank-placement-request/v1', status='complete',
                      fpga=fpga, policy=POLICY, banks=len(banks), rx_projections=len(receive),
                      directory=str(output),
                      input_sha256={str(parent.path): file_sha256(parent.path) for parent in parents},
                      files={name: dict(path=str(output / name), sha256=file_sha256(staging / name))
                             for name in ('banks.tsv', 'rx-bank-roles.tsv')})
        write_json(staging / 'request.json', result)
        require_store_sources_unchanged(parents)
        staging.rename(output)
    return result
