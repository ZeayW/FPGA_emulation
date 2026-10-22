#!/usr/bin/env python3
"""Replay bounded Phase3 qualification without rerunning partitioning.

Producer reports remain unchanged. This checks the fixed original STA
representative population and independently rebuilds the structural cut contract.
It is not all-path STA or routed timing signoff.
"""
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'scripts')]
from phase4_nvdla_compare import (CLOCKS, check_producer, code_identity, digest,
    read, require, runtime_identity, stat_identity, verify_provenance, write)
from phase4_nvdla_timing import validate_original_projection


def qualify_common(common, provenance, *, architecture_timing_db=None, timing_model=None):
    from emuflow.io import read_json
    from emuflow.phase3_storage import read_phase3_cut_timing_view
    from emuflow.indexed_physical_ir import IndexedPhysicalIR
    from emuflow.sta import iter_sta_path_database_paths
    from emuflow.cut_segment_qualification import validate_cut_segment_qualification
    from emuflow.opensta import DEFAULT_TIMING_MODEL
    import sqlite3

    report_path = common / 'multi-fpga-stage-report.json'
    report = read(report_path)
    check_producer(report, provenance)
    require(report.get('completed_through') == 'phase3' and
            report.get('validation', {}).get('status') == 'pass', 'completed Phase3 required')
    artifacts = {k: common / v['path'] for k, v in report['artifacts'].items()}
    assignment_path = artifacts['assignment']
    index = assignment_path.with_name('assignment.sqlite')
    with sqlite3.connect(f'file:{index.resolve()}?mode=ro&immutable=1', uri=True) as db:
        require(db.execute('SELECT 1 FROM copies WHERE is_replica=1 LIMIT 1').fetchone() is None,
                'replicated cell ownership requires a separately qualified traversal')
    original_path = artifacts['timing_path_database']
    original = read_json(original_path)
    structural_path = common / 'timing/cut-segment-qualification.json'
    timing_model = timing_model or DEFAULT_TIMING_MODEL
    files = {report_path, assignment_path, index, artifacts['ir'], original_path,
             artifacts['timing_paths'], structural_path, Path(timing_model)}
    for descriptor in original.get('payloads', {}).values():
        relative = Path(descriptor['path'])
        require(not relative.is_absolute() and relative.name == str(relative), 'invalid timing sidecar')
        files.add(original_path.parent / relative)
    if architecture_timing_db:
        require(provenance['files'].get(str(architecture_timing_db.resolve())) == digest(architecture_timing_db),
                'architecture timing database not sealed in producer provenance')
        files.add(architecture_timing_db)
    print("qualification: sealing immutable input files", file=sys.stderr, flush=True)
    seals = {str(p.resolve()): digest(p) for p in files}
    before = stat_identity(seals)
    assignment = read_phase3_cut_timing_view(assignment_path, include_instance_assignment=True)
    owners = assignment["instance_assignment"]
    try:
        print("qualification: original member/clock/ownership accounting", file=sys.stderr, flush=True)
        with IndexedPhysicalIR(artifacts['ir']) as ir:
            accounting = validate_original_projection(
                iter_sta_path_database_paths(original_path, original, verify_payload=False),
                assignment, read_json(artifacts['timing_paths']), original_nets=ir.nets)
        require({k: v['period_ns'] for k, v in accounting['per_clock'].items()} == CLOCKS,
                'original two-clock 10 ns coverage differs')
        projection = report['timing']['cut_path_projection']
        for key in ('cut_nets', 'covered_cut_nets', 'uncovered_cut_nets'):
            require(accounting[key] == projection[key], f'projection count differs: {key}')
        require(accounting['original_paths'] == projection['database_paths'] == report['timing']['sta']['paths'],
                'original population count differs')
        require(accounting['projected_members'] == projection['projected_paths'], 'projected population count differs')
        # Release millions of cut records before the independent structural
        # validator loads its own assignment view.
        assignment = None
        if hasattr(owners, "connection"):
            owners.connection.close()
        print("qualification: reconstructing full-cut structural contract", file=sys.stderr, flush=True)
        structural = validate_cut_segment_qualification(read_json(structural_path), artifacts['ir'],
            assignment_path, original_path, timing_model_path=timing_model,
            architecture_timing_db_path=architecture_timing_db)
        require(structural['cut_nets'] == accounting['cut_nets'], 'structural cut coverage differs')
        require(stat_identity(seals) == before, 'qualification inputs changed during replay')
        return {'schema': 'emuflow.nvdla-prefix-qualification/v1', 'status': 'pass',
            'producer_source_sha256': provenance['source_sha256'],
            'evaluator_source_sha256': code_identity(), 'evaluator_runtime': runtime_identity(),
            'evaluator_sha256': {str(p.resolve()): digest(p) for p in
                [Path(__file__), ROOT / 'scripts/phase4_nvdla_timing.py']},
            'common_run': str(common.resolve()), 'files': seals,
            'original_accounting': accounting, 'structural_qualification': structural,
            'qualification_level': 'artifact accounting and structure only; no convergence or formal study-result claim',
            'timing_scope': 'fixed-original-representative-population; preplacement estimates only; not all possible paths'}
    finally:
        if hasattr(owners, 'connection'):
            owners.connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--common-run', type=Path, required=True)
    parser.add_argument('--provenance', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--architecture-timing-db', type=Path)
    parser.add_argument('--timing-model', type=Path)
    args = parser.parse_args()
    require(not args.output.exists(), 'qualification output must be new')
    provenance = read(args.provenance)
    verify_provenance(provenance, replay_source=code_identity())
    result = qualify_common(args.common_run, provenance,
        architecture_timing_db=args.architecture_timing_db, timing_model=args.timing_model)
    result['command'] = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]
    write(args.output, result)


if __name__ == '__main__':
    main()
