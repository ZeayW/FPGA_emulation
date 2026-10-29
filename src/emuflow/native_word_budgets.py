"""Convert complete transport arrivals into native RX-to-capture budgets.

This visits the bounded transport interface, never the DUT graph. The output
belongs to a disposable timing view; implementation constraints are unchanged.
"""
import csv
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import shutil
import tempfile

from .errors import ValidationError
from .native_word_analysis import INDEX_SCHEMA
from .store_publication import bind_store_source, require_store_sources_unchanged, validate_store_destination


ARRIVAL_HEADER = ('window', 'launch_clock_hex', 'launch_edge', 'capture_clock_hex', 'capture_edge',
    'reference_window_ps', 'transfer_hex', 'source_partition_hex', 'sink_partition_hex',
    'arrival_status', 'maximum_arrival_ps')
BUDGET_HEADER = ('fpga_hex', 'source_origin', 'capture_clock_hex', 'capture_edge', 'budget_ps',
    'launch_clock_hex', 'launch_edge', 'reference_window_ps', 'arrival_ps')


def _require(condition, message):
    if not condition:
        raise ValidationError(message)


def _integer(text):
    try:
        value = int(text)
    except (TypeError, ValueError):
        raise ValidationError('native word arrival coordinate is invalid') from None
    _require(str(value) == text and -(1 << 63) <= value < (1 << 63),
             'native word arrival coordinate exceeds its signed domain')
    return value


def _identity(text):
    try:
        data = bytes.fromhex(text)
        value = data.decode('utf-8')
    except (TypeError, ValueError, UnicodeError):
        raise ValidationError('native word arrival identity is invalid') from None
    _require(data.hex() == text and bool(value) and '\0' not in value,
             'native word arrival identity is not canonical')
    return value


def write_native_word_budgets(index, analysis, arrivals, output):
    """Stream source-bound budgets, minimizing over logical launch families.

    For each native RX/capture-clock/edge, B = min(window - RX arrival).
    Negative budgets retain violations. Each endpoint can have a different
    worst phase; this does not assert a simultaneous phase for endpoint TNS.
    No-causal-launch rows contribute no constraint and never become zeros.
    Every arrival row must match the complete ordered transfer/window census.
    """
    index, analysis, arrivals, output = (Path(p).resolve() for p in (index, analysis, arrivals, output))
    parents = tuple(bind_store_source(p, schema) for p, schema in (
        (index, INDEX_SCHEMA), (analysis, 'native-word-network-analysis-v1'),
        (arrivals, 'native-word-arrival-bounds-v1')))
    validate_store_destination(output, parents)
    receipt = Path(str(output)+'.sources.json')
    validate_store_destination(receipt, parents)
    model = json.loads(analysis.read_text())
    _require(model.get('schema') == 'emuflow.native-word-network-analysis/v1' and
             model.get('status') == 'setup-model-projected', 'native word arrival analysis is incomplete')
    output.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(index.as_uri()+'?mode=ro&immutable=1', uri=True)) as source, \
            tempfile.TemporaryDirectory(prefix='.native-word-budgets-', dir=output.parent) as temporary:
        temporary = Path(temporary)
        with closing(sqlite3.connect(temporary/'budgets.sqlite')) as reduced, reduced:
            reduced.execute('CREATE TABLE budgets(fpga TEXT,origin TEXT,clock TEXT,edge INTEGER,budget INTEGER,'
                'launch TEXT,launch_edge INTEGER,window INTEGER,arrival INTEGER,PRIMARY KEY(fpga,origin,clock,edge)) WITHOUT ROWID')
            transfers = list(source.execute('SELECT id,source_partition,sink_partition FROM transfers ORDER BY id'))
            _require([r[0] for r in transfers] == model.get('transfer_order'),
                     'native word arrival transfer census differs')
            rx = {(fpga, entry): origin for fpga, origin, entry in source.execute(
                'SELECT fpga,origin,entry_id FROM rx_entries')}
            aliases = {}
            for fpga, native, logical in source.execute('SELECT fpga,native_clock,logical_clock FROM clocks'):
                aliases.setdefault((fpga, logical), []).append(native)
            windows = model.get('windows')
            _require(isinstance(windows, list) and bool(windows), 'native word arrival windows are absent')
            declared = set(source.execute('SELECT launch_clock,launch_edge,capture_clock,capture_edge,reference_window_ps FROM windows'))
            seen = set()
            with arrivals.open(newline='') as stream:
                reader = csv.DictReader(stream, delimiter='\t')
                _require(tuple(reader.fieldnames or ()) == ARRIVAL_HEADER, 'native word arrival header differs')
                for number, window in enumerate(windows):
                    key = tuple(window[k] for k in ('launch_clock','launch_edge','capture_clock','capture_edge','reference_window_ps'))
                    _require(key in declared and key not in seen, 'native word arrival window owner differs')
                    seen.add(key)
                    for identifier, sender, receiver in transfers:
                        row = next(reader, None)
                        _require(row is not None and None not in row and all(v is not None for v in row.values()),
                                 'native word arrival census is truncated or malformed')
                        _require(_integer(row['window']) == number and
                            (_identity(row['launch_clock_hex']), _integer(row['launch_edge']),
                             _identity(row['capture_clock_hex']), _integer(row['capture_edge']),
                             _integer(row['reference_window_ps'])) == key and
                            (_identity(row['transfer_hex']), _identity(row['source_partition_hex']),
                             _identity(row['sink_partition_hex'])) == (identifier, sender, receiver),
                            'native word arrival row belongs to another transfer or window')
                        if row['arrival_status'] == 'no-causal-launch':
                            _require(row['maximum_arrival_ps'] == '', 'absent native launch contains a numerical arrival')
                            continue
                        _require(row['arrival_status'] == 'reachable', 'native word arrival disposition is invalid')
                        arrival = _integer(row['maximum_arrival_ps'])
                        budget = _integer(str(key[4]-arrival))
                        _require((receiver, identifier) in rx, 'native word RX owner is absent')
                        for native in aliases.get((receiver, key[2]), ()):
                            reduced.execute('INSERT INTO budgets VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(fpga,origin,clock,edge) '
                                'DO UPDATE SET budget=excluded.budget,launch=excluded.launch,launch_edge=excluded.launch_edge,'
                                'window=excluded.window,arrival=excluded.arrival WHERE excluded.budget<budgets.budget',
                                (receiver, rx[receiver, identifier], native, key[3], budget, key[0], key[1], key[4], arrival))
                _require(next(reader, None) is None, 'native word arrival census has extra rows')
            destination = temporary/'budgets.tsv'
            with destination.open('w', newline='') as stream:
                writer = csv.writer(stream, delimiter='\t', lineterminator='\n')
                writer.writerow(BUDGET_HEADER)
                for r in reduced.execute('SELECT * FROM budgets ORDER BY fpga,origin,clock,edge'):
                    writer.writerow((r[0].encode().hex(), r[1], r[2].encode().hex(), r[3], r[4],
                                     r[5].encode().hex(), r[6], r[7], r[8]))
            from .io import file_sha256
            provenance = dict(schema='emuflow.native-word-rx-budgets/v1',
                budget=dict(path=str(output), sha256=file_sha256(destination)),
                recipe_sha256=file_sha256(Path(__file__)),
                sources=[dict(path=str(p.path),schema=p.schema,generation=list(p.generation),
                              sha256=file_sha256(p.path)) for p in parents])
            receipt_temporary = temporary/'sources.json'
            receipt_temporary.write_text(json.dumps(provenance,sort_keys=True,indent=2)+'\n')
            require_store_sources_unchanged(parents)
            destination.replace(output)
            receipt_temporary.replace(receipt)
    return output


def observe_native_word_budgets(record, budgets, output, *, executable=None, threads=2):
    """Measure every timed native payload endpoint after applying RX budgets.

    Original state names/types are not query endpoints. The original routed
    DCP is read-only, and no optimization, routing or clock creation is run.
    Startup/control membership and original check dispositions stay separate.
    """
    from .io import file_sha256, write_json
    from .vivado_backend import _resolve_vivado
    from .vivado_physical import _vivado_script, execute_vivado
    from .store_publication import StoreSource
    _require(type(threads) is int and threads > 0, 'native endpoint observation thread count is invalid')
    observation = record['observation']
    _require(observation.get('status') == 'complete', 'native endpoint observation requires a closed routed source')
    fpga = observation['fpga']
    checkpoint = Path(observation['checkpoint']['path']).resolve()
    roles = Path(observation['source_directory']).resolve()
    _require(record['sources']['directory'] == str(roles), 'native endpoint role owner differs')
    contract = Path(record['clock_contract']['path']).resolve()
    budgets = Path(budgets).resolve()
    output = Path(output).resolve()
    _require(not output.exists(), 'native endpoint observation requires a fresh output directory')
    parents = [StoreSource(Path(p['path']), p['schema'], tuple(p['generation']))
               for p in observation['source_generations']]
    receipt = Path(str(budgets)+'.sources.json')
    parents.append(bind_store_source(receipt, 'physical-input-document-v1'))
    provenance = json.loads(receipt.read_text())
    _require(provenance.get('schema') == 'emuflow.native-word-rx-budgets/v1' and
             provenance.get('budget') == dict(path=str(budgets),sha256=file_sha256(budgets)),
             'native endpoint budgets lack their source-bound receipt')
    for p in provenance['sources']:
        parent = StoreSource(Path(p['path']),p['schema'],tuple(p['generation']))
        require_store_sources_unchanged((parent,))
        _require(file_sha256(parent.path) == p['sha256'], 'native endpoint budget source changed')
        parents.append(parent)
    indexes = [Path(p['path']) for p in provenance['sources'] if p['schema'] == INDEX_SCHEMA]
    _require(len(indexes) == 1, 'native endpoint budget index owner is absent or repeated')
    with closing(sqlite3.connect(indexes[0].as_uri()+'?mode=ro&immutable=1', uri=True)) as index:
        row = index.execute("SELECT value FROM metadata WHERE key='source_generations'").fetchone()
    _require(row is not None, 'native endpoint budget index lacks its source bindings')
    catalog_parents = {Path(p['path']).resolve(): StoreSource(Path(p['path']),p['schema'],tuple(p['generation']))
                       for p in json.loads(row[0])}
    for path in (contract, roles/'catalog.sqlite', Path(observation['files']['native-capture-costs.tsv']['path'])):
        path = path.resolve()
        _require(path in catalog_parents, 'native endpoint budgets belong to a different routed observation')
        parents.append(catalog_parents[path])
    for p in (checkpoint, contract, roles/'rx-roles.tsv', budgets):
        parents.append(bind_store_source(p, 'physical-input-document-v1'))
    _require(file_sha256(checkpoint) == observation['checkpoint']['sha256'] and
             file_sha256(contract) == record['clock_contract']['sha256'], 'native endpoint snapshot or clock contract changed')
    names = ('observe_native_word_budgets.tcl','native_word_budgets.tcl','native_word_timing.tcl',
             'word_phase_context.tcl','timing_contract.tcl','native_session.tcl')
    recipes = [bind_store_source(_vivado_script(name), 'physical-input-document-v1') for name in names]
    require_store_sources_unchanged((*parents, *recipes))
    output.mkdir(parents=True)
    (output/'recipe').mkdir()
    for name, recipe in zip(names, recipes):
        shutil.copyfile(recipe.path, output/'recipe'/name)
        parents.append(bind_store_source(output/'recipe'/name, 'physical-input-document-v1'))
    execution = execute_vivado(_resolve_vivado(executable), output/'recipe'/names[0],
        [str(checkpoint), str(roles), str(budgets), fpga, str(contract), str(output), str(threads)],
        output, 'native-endpoint-observation.log')
    require_store_sources_unchanged((*parents, *recipes))
    _require(file_sha256(checkpoint) == observation['checkpoint']['sha256'], 'native endpoint view changed its checkpoint')
    metrics, timing_summary, files = _summarize_native_endpoints(output, scratch_parent=output)
    result = dict(schema='emuflow.native-word-endpoint-timing/v1', fpga=fpga,
        status='incomplete' if metrics['missing_native_rx_budgets'] else 'complete', metrics=metrics,
        summary=timing_summary,
        scope='Worst continuous-phase payload check per native endpoint and capture clock; endpoint TNS is an envelope over individually worst phases, not a single shared phase',
        original_path_dispositions='separate', startup_and_minimum_acceptance='separate',
        checkpoint=observation['checkpoint'], files=files, execution=execution,
        sources=[dict(path=str(p.path),schema=p.schema,generation=list(p.generation)) for p in parents])
    require_store_sources_unchanged(parents)
    write_json(output/'native-endpoint-observation.json', result)
    return result


def _summarize_native_endpoints(directory, *, scratch_parent=None):
    from .io import file_sha256
    metrics_path = directory/'native-endpoint-metrics.tsv'
    with metrics_path.open(newline='') as stream:
        metrics = {}
        reader = csv.DictReader(stream, delimiter='\t')
        _require(tuple(reader.fieldnames or ()) == ('metric','value'), 'native endpoint metric header differs')
        for row in reader:
            _require(None not in row and all(v is not None for v in row.values()), 'native endpoint metric row is malformed')
            _require(row['metric'] not in metrics, 'native endpoint metric repeats')
            metrics[row['metric']] = _integer(row['value'])
    _require(set(metrics) == {'target_pin_upper_bound','endpoint_checks','capture_clocks',
        'applied_rx_budgets','missing_native_rx_budgets','reset_ceiling_ps'} and all(v >= 0 for v in metrics.values()),
        'native endpoint query census is invalid')
    _require(metrics['endpoint_checks'] <= metrics['target_pin_upper_bound']*metrics['capture_clocks'],
             'native endpoint query exceeds its native target census')
    count = failing = 0
    worst = None
    negative = 0
    endpoints = directory/'native-endpoint-slacks.tsv'
    # A temporary primary key verifies the producer's per-endpoint/capture
    # de-duplication without materializing a DUT-sized Python map.
    with tempfile.TemporaryDirectory(prefix='.native-endpoint-census-', dir=scratch_parent) as temporary:
        with closing(sqlite3.connect(str(Path(temporary)/'census.sqlite'))) as census, census:
            census.execute('CREATE TABLE endpoints(pin TEXT,clock TEXT,PRIMARY KEY(pin,clock)) WITHOUT ROWID')
            with endpoints.open(newline='') as stream:
                reader = csv.DictReader(stream, delimiter='\t')
                expected = ('endpoint_hex','capture_clock_hex','slack_ns','data_on_worst_slack_path_ns',
                            'start_hex','launch_clock_hex','requirement_ns','launch_edge_ns','capture_edge_ns')
                _require(tuple(reader.fieldnames or ()) == expected, 'native endpoint timing header differs')
                for row in reader:
                    _require(None not in row and all(v is not None for v in row.values()), 'native endpoint timing row is malformed')
                    for name in ('endpoint_hex','capture_clock_hex','start_hex','launch_clock_hex'):
                        _identity(row[name])
                    try:
                        values = [float(row[name]) for name in ('slack_ns','data_on_worst_slack_path_ns',
                            'requirement_ns','launch_edge_ns','capture_edge_ns')]
                    except ValueError:
                        raise ValidationError('native endpoint timing value is invalid') from None
                    _require(all(math.isfinite(v) for v in values) and values[1] >= 0, 'native endpoint timing value is invalid')
                    slack = round(values[0]*1000)
                    _require(abs(slack/1000-values[0]) < 1e-8, 'native endpoint slack is finer than its provider grid')
                    try:
                        census.execute('INSERT INTO endpoints VALUES(?,?)', (row['endpoint_hex'],row['capture_clock_hex']))
                    except sqlite3.IntegrityError:
                        raise ValidationError('native endpoint capture check repeats') from None
                    count += 1
                    worst = slack if worst is None else min(worst, slack)
                    if slack < 0:
                        failing += 1
                        negative += slack
    _require(count == metrics['endpoint_checks'], 'native endpoint timing census is truncated')
    with (directory/'missing-budget-rx.tsv').open(newline='') as stream:
        reader = csv.DictReader(stream, delimiter='\t')
        _require(tuple(reader.fieldnames or ()) == ('source_origin','capture_clock_hex','capture_edge'),
                 'missing native budget header differs')
        missing = 0
        for row in reader:
            _identity(row['capture_clock_hex'])
            _require(row['source_origin'].startswith('s') and row['source_origin'][1:].isdigit() and
                     row['capture_edge'] in ('1','2'), 'missing native budget record is invalid')
            missing += 1
        _require(missing == metrics['missing_native_rx_budgets'], 'missing native budget census is truncated')
    files = {p.name: {'path': str(p), 'sha256': file_sha256(p)} for p in
             (endpoints, metrics_path, directory/'missing-budget-rx.tsv')}
    return metrics, dict(endpoint_clock_checks=count, failing_checks=failing,
                         wns_ns=None if worst is None else worst/1000,
                         tns_ns=None if worst is None else negative/1000), files


def read_native_word_budgets(record, directory, *, expected_budgets=None):
    """Recheck retained native endpoint measurements without invoking Vivado.

    Raw files may be relocated byte-for-byte; original input and recipe
    generations remain their provenance. Counts and WNS/TNS are recomputed.
    """
    from .io import file_sha256
    from .store_publication import StoreSource
    directory = Path(directory).resolve()
    receipt = directory/'native-endpoint-observation.json'
    parents = [bind_store_source(receipt, 'physical-input-document-v1')]
    value = json.loads(receipt.read_text())
    _require(record['observation'].get('status') == 'complete' and
             value.get('schema') == 'emuflow.native-word-endpoint-timing/v1' and
             value.get('fpga') == record['observation']['fpga'] and
             value.get('checkpoint',{}).get('sha256') == record['observation']['checkpoint']['sha256'],
             'saved native endpoint observation belongs to another snapshot')
    parents.extend(StoreSource(Path(p['path']),p['schema'],tuple(p['generation'])) for p in value['sources'])
    require_store_sources_unchanged(parents)
    if expected_budgets is not None:
        expected_budgets = Path(expected_budgets).resolve()
        parents.append(bind_store_source(expected_budgets, 'physical-input-document-v1'))
        receipts = []
        for parent in parents:
            if parent.path.name.endswith('.sources.json'):
                candidate = json.loads(parent.path.read_text())
                if candidate.get('schema') == 'emuflow.native-word-rx-budgets/v1':
                    receipts.append(candidate)
        _require(len(receipts) == 1 and receipts[0]['budget']['sha256'] == file_sha256(expected_budgets),
                 'saved native endpoint observation used different RX budgets')
    _require(set(value['files']) == {'native-endpoint-slacks.tsv','native-endpoint-metrics.tsv','missing-budget-rx.tsv'},
             'saved native endpoint file roles differ')
    for name, reference in value['files'].items():
        path = directory/name
        parents.append(bind_store_source(path, 'physical-input-document-v1'))
        _require(file_sha256(path) == reference['sha256'], 'saved native endpoint measurements changed')
    if value.get('execution') is not None:
        execution = value['execution']
        log = directory/Path(execution['log']).name
        parents.append(bind_store_source(log, 'physical-input-document-v1'))
        _require(execution.get('status') == 'pass' and file_sha256(log) == execution['log_sha256'],
                 'saved native endpoint execution evidence changed')
    metrics, timing_summary, _files = _summarize_native_endpoints(directory)
    _require(metrics == value['metrics'] and timing_summary == value['summary'] and
             value['status'] == ('incomplete' if metrics['missing_native_rx_budgets'] else 'complete'),
             'saved native endpoint summary disagrees with its raw measurements')
    require_store_sources_unchanged(parents)
    return value


def _qualify_missing_native_rx(record, directory):
    """Prove absent RX roles from closed source cones, without assigning delay.

    A missing source tag is insufficient. Recompute the conservative closure
    against the sealed mapped input and routed lineage on every replay, so a
    saved status document cannot turn an unproved source into an elimination.
    """
    from .io import file_sha256, write_json
    from .vivado_optimization import build_optimization_proofs, OptimizationProofView
    with (directory/'missing-budget-rx.tsv').open(newline='') as stream:
        rows = list(csv.DictReader(stream, delimiter='\t'))
    origins = sorted({row['source_origin'] for row in rows})
    for origin in origins:
        _require(origin == 's'+str(int(origin[1:])) and int(origin[1:]) <= (1 << 63)-1,
                 'missing native RX origin is not canonical')
    dispositions = []
    proof_reference = None
    if origins:
        mapped = Path(record['input_identity']['mapped_index']['path']).resolve()
        lineage = Path(record['lineage']['path']).resolve()
        parents = (bind_store_source(mapped, 'xilinx-mapped-index-v2'),
                   bind_store_source(lineage, 'vivado-lineage-store-v2'))
        _require(file_sha256(mapped) == record['input_identity']['mapped_index']['sha256'] and
                 file_sha256(lineage) == record['lineage']['sha256'],
                 'native RX elimination uses a different physical source')
        proof = directory/'missing-rx-optimization.sqlite'
        facts = build_optimization_proofs(mapped, lineage, proof,
            mapped_sha256=record['input_identity']['mapped_index']['sha256'],
            fpga=record['observation']['fpga'], stage='routed', unobservable_only=True)
        with OptimizationProofView(proof, expected_fpga=record['observation']['fpga']) as view:
            for origin in origins:
                row = view.connection.execute('SELECT reason FROM eliminated_sources WHERE origin=?',
                                              (int(origin[1:]),)).fetchone()
                if row == ('unobservable-source-closure',):
                    fact = view.require_eliminated_source(origin)
                    dispositions.append(dict(source_origin=origin, status='optimized-away',
                        reason=fact['reason'], source_cell=fact['source_cell'], delay_ns=None))
                else:
                    dispositions.append(dict(source_origin=origin, status='unresolved', delay_ns=None))
            view.require_unchanged()
        require_store_sources_unchanged(parents)
        proof_reference = dict(artifact=proof.name, proof_rows_sha256=facts['proof_rows_sha256'],
            mapped_index_sha256=record['input_identity']['mapped_index']['sha256'],
            lineage_sha256=record['lineage']['sha256'])
    proved = sum(row['status']=='optimized-away' for row in dispositions)
    result = dict(status='complete' if proved == len(origins) else 'incomplete',
        missing_budget_checks=len(rows), missing_origins=len(origins), optimized_away_origins=proved,
        unresolved_origins=len(origins)-proved, dispositions=dispositions,
        scope='Closed unobservable source cones only; no constant-state or initialization assumptions and no numerical delay')
    if proof_reference is not None:
        result['proof'] = proof_reference
    write_json(directory/'missing-rx-dispositions.json', result)
    return result


def collect_native_word_endpoint_timing(records, budgets, output, *, executable=None, results_dir=None):
    """Collect all partitions with one policy, or recheck retained raw results.

    Saved measurements are copied into the new runtime after validation, so
    bundle replay can use the same files without invoking a vendor process.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .io import file_sha256, write_json
    output = Path(output).resolve()
    _require(not output.exists(), 'native endpoint collection requires a fresh directory')
    output.mkdir(parents=True)
    destinations = {name: (output/name).resolve() for name in records}
    _require(all(path.parent == output for path in destinations.values()), 'native endpoint partition identity is invalid')
    observed = {}
    if results_dir is None:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = {pool.submit(observe_native_word_budgets, record, budgets, destinations[name],
                                  executable=executable): name for name, record in records.items()}
            for future in as_completed(pending):
                observed[pending[future]] = future.result()
    else:
        results_dir = Path(results_dir).resolve()
        for name, record in records.items():
            saved = results_dir/name
            value = read_native_word_budgets(record, saved, expected_budgets=budgets)
            names = (*value['files'], 'native-endpoint-observation.json',
                     *((Path(value['execution']['log']).name,) if value.get('execution') is not None else ()))
            parents = [bind_store_source(saved/n, 'physical-input-document-v1') for n in names]
            destinations[name].mkdir()
            for filename in names:
                shutil.copyfile(saved/filename, destinations[name]/filename)
                _require(file_sha256(saved/filename) == file_sha256(destinations[name]/filename),
                         'retained native endpoint measurements changed during copy')
            require_store_sources_unchanged(parents)
            observed[name] = value
    coverage = {name: _qualify_missing_native_rx(records[name], destinations[name]) for name in observed}
    summaries = [value['summary'] for value in observed.values()]
    worst = [value['wns_ns'] for value in summaries if value['wns_ns'] is not None]
    result = dict(schema='emuflow.native-word-endpoint-collection/v1',
        status='complete' if all(value['status']=='complete' for value in coverage.values()) else 'incomplete',
        scope='Per-native-endpoint/capture-clock worst-phase payload envelope; startup, minimum timing and original check dispositions remain separate',
        summary=dict(endpoint_clock_checks=sum(value['endpoint_clock_checks'] for value in summaries),
                     failing_checks=sum(value['failing_checks'] for value in summaries),
                     wns_ns=min(worst) if worst else None,
                     tns_ns=sum(round(value['tns_ns']*1000) for value in summaries if value['tns_ns'] is not None)/1000 if worst else None),
        partitions={name: dict(status=coverage[name]['status'],raw_observation_status=value['status'],
            rx_coverage=coverage[name],metrics=value['metrics'],summary=value['summary'],
            artifact=name+'/native-endpoint-observation.json',
            sha256=file_sha256(destinations[name]/'native-endpoint-observation.json')) for name,value in sorted(observed.items())})
    write_json(output/'summary.json', result)
    return result
