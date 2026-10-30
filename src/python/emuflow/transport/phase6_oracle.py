"""Independent, size-limited behavioral qualification for native Phase 6."""
from __future__ import annotations

import hashlib
from typing import Any, Dict, Mapping, Optional

from emuflow.common.errors import ValidationError
from emuflow.ir.ir import EmuIR
from emuflow.validation.equivalence import (
    exhaustively_verify_static_exact_partition_equivalence,
    simulate_static_exact_partition_equivalence,
)

import json
from pathlib import Path
import sys


def _static_exact_equivalence_evidence(
    ir: EmuIR,
    assignment: Mapping[str, Any],
    schedule: Mapping[str, Any],
    *,
    cycles: int,
    seed: int,
    clock_periods: Optional[Mapping[str, float]],
) -> Dict[str, Any]:
    random_traces = [
        simulate_static_exact_partition_equivalence(
            ir,
            assignment,
            schedule,
            cycles=cycles,
            seed=seed + offset,
            clock_periods=clock_periods,
        )
        for offset in range(3)
    ]
    exhaustive = None
    exhaustive_skip = None
    try:
        exhaustive = exhaustively_verify_static_exact_partition_equivalence(
            ir,
            assignment,
            schedule,
            max_variables=12,
            clock_periods=clock_periods,
        )
    except ValidationError as error:
        message = str(error)
        if (
            "variable limit exceeded" not in message
            and "does not support memory state" not in message
        ):
            raise
        exhaustive_skip = {
            "status": "not-run",
            "reason": message,
            "qualification": "large-model-boundary-not-a-proof",
        }
    evidence = {
        "status": "pass",
        "provider": "static-exact-event-driven-macro-cycle-equivalence-v1",
        "semantic_contract_sha256": schedule[
            "semantic_contract_sha256"
        ],
        "random_traces": random_traces,
        "random_trace_count": len(random_traces),
        "random_macro_cycles": sum(item["cycles"] for item in random_traces),
        "cycles": sum(item["cycles"] for item in random_traces),
        "compared_state_bits": sum(
            item["compared_state_bits"] for item in random_traces
        ),
        "compared_output_bits": sum(
            item["compared_output_bits"] for item in random_traces
        ),
        "source_full_evaluations": sum(
            item["source_full_evaluations"] for item in random_traces
        ),
        "reference_full_evaluations": sum(
            item["reference_full_evaluations"] for item in random_traces
        ),
        "initialization_full_evaluations": sum(
            item["initialization_full_evaluations"]
            for item in random_traces
        ),
        "partition_full_evaluations": sum(
            item["partition_full_evaluations"] for item in random_traces
        ),
        "incremental_combinational_cell_evaluations": sum(
            item["incremental_combinational_cell_evaluations"]
            for item in random_traces
        ),
        "shadow_pin_updates": sum(
            item["shadow_pin_updates"] for item in random_traces
        ),
        "mismatches": 0,
        "trace_sha256": hashlib.sha256(
            json.dumps(
                [item["trace_sha256"] for item in random_traces],
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "assumptions": [
            "one-commit macro-step semantics",
            "reset is deasserted during each checked macro-step",
            "transport shadows begin unavailable and must be produced in-frame",
        ],
    }
    if exhaustive is not None:
        evidence.update(
            {
                "qualification": (
                    "exhaustive-small-model-proof-plus-random-traces"
                ),
                "exhaustive_macro_step": exhaustive,
            }
        )
    else:
        evidence.update(
            {
                "qualification": (
                    "multi-seed-random-trace-validation-not-proof"
                ),
                "exhaustive_macro_step": exhaustive_skip,
            }
        )
    return evidence



def qualify(request_path, output):
    from emuflow.common.errors import ValidationError
    from emuflow.ir.ir import EmuIR
    from emuflow.ir.emuir_compact import read_compact_emuir
    from emuflow.common.io import write_json
    from emuflow.validation.equivalence import simulate_partition_equivalence
    from emuflow.transport.split_netlist_storage import read_compact_emuir_control_metadata
    from emuflow.scheduling.tdm import is_sampled_virtual_wire_schedule
    from emuflow.scheduling.tdm_schedule_storage import read_tdm_schedule
    from emuflow.storage.store_publication import require_store_sources_unchanged
    request_path=Path(request_path)
    if request_path.stat().st_size>1024*1024:
        raise ValidationError('small oracle request exceeds bound')
    request=json.loads(request_path.read_text())
    if request.get('schema')!='emuflow.phase6-small-oracle/v1':
        raise ValidationError('unsupported small oracle request')
    from emuflow.storage.store_publication import StoreSource
    refs=tuple(StoreSource(Path(row['path']),row['schema'],tuple(row['generation']))
               for row in request['sources'])
    require_store_sources_unchanged(refs)
    source=read_compact_emuir_control_metadata(request['ir'])
    counts=source['record_counts']
    if counts['cell_records']>10000 or counts['endpoint_records']>100000:
        raise ValidationError('behavioral oracle is limited to independent small-design qualification')
    schedule=read_tdm_schedule(Path(request['schedule']),materialize=True)
    assignment=read_bounded_assignment(Path(request['ir']),Path(request['assignment']))
    ir=EmuIR(read_compact_emuir(Path(request['ir'])))
    if is_sampled_virtual_wire_schedule(schedule):
        result=_static_exact_equivalence_evidence(ir,assignment,schedule,
            cycles=request['cycles'],seed=request['seed'],clock_periods=request['clock_periods'])
    else:
        result=simulate_partition_equivalence(ir,assignment,schedule,
            cycles=request['cycles'],seed=request['seed'])
    require_store_sources_unchanged(refs)
    write_json(Path(output),result,compact=True)


def read_bounded_assignment(ir_path, assignment_path):
    """Read current typed facts only for the independent small-design oracle.

    This deliberately bounded qualification reader is never the Phase 3--7
    production data channel. It cannot read historical JSON/cluster snapshots.
    """
    from emuflow.common.errors import ValidationError
    from emuflow.common.native_tools import native_file_generation
    from emuflow.transport.split_netlist_storage import read_compact_emuir_control_metadata
    from emuflow.partition.phase3_storage import read_phase3_assignment_view
    from emuflow.partition.phase3_assignment_index import open_phase3_assignment_index, _exact_binary_reference
    from emuflow.partition.phase3_exact_binary import _load_manifest, iter_phase3_exact_segments, iter_phase3_exact_captures, iter_phase3_exact_node_sources
    from emuflow.partition.combinational_cut import semantic_contract_sha256

    ir_path, assignment_path = Path(ir_path).resolve(), Path(assignment_path).resolve()
    paths = {path:native_file_generation(path) for path in (ir_path, assignment_path)}
    source = read_compact_emuir_control_metadata(ir_path)
    counts = source['record_counts']
    if counts['cell_records'] > 10000 or counts['endpoint_records'] > 100000:
        raise ValidationError('behavioral oracle is limited to independent small-design qualification')
    if ir_path.stat().st_size > 256*1024*1024 or assignment_path.stat().st_size > 1024*1024:
        raise ValidationError('behavioral oracle input exceeds byte bound')
    selector = json.loads(assignment_path.read_text(encoding='utf-8'))
    artifact = (selector.get('cut_nets_index') or {}).get('artifact')
    if not isinstance(artifact, str) or Path(artifact).name != artifact:
        raise ValidationError('behavioral oracle requires current typed assignment selector')
    index = assignment_path.with_name(artifact)
    if index.stat().st_size > 64*1024*1024:
        raise ValidationError('behavioral oracle assignment exceeds byte bound')
    paths[index] = native_file_generation(index)
    view = read_phase3_assignment_view(assignment_path, include_instance_assignment=True)
    owners = view['instance_assignment']
    index = owners.path
    try:
        if len(owners) != counts['cell_records'] or len(view['cut_nets']) > 100000:
            raise ValidationError('behavioral oracle assignment population disagrees')
        result = {**view, 'instance_assignment':dict(owners), 'cut_nets':list(view['cut_nets'])}
    finally:
        owners.close()
    digest = result.get('semantic_contract_sha256')
    if digest is not None:
        connection = open_phase3_assignment_index(index)
        try:
            binary, descriptor = _exact_binary_reference(connection, index)
        finally:
            connection.close()
        paths[binary] = native_file_generation(binary)
        if binary.stat().st_size > 64*1024*1024:
            raise ValidationError('behavioral oracle exact facts exceed byte bound')
        manifest = _load_manifest(binary, descriptor)
        if sum(manifest['counts'].values()) > 1000000:
            raise ValidationError('behavioral oracle exact facts exceed record bound')
        segments = list(iter_phase3_exact_segments(binary, descriptor))
        captures = list(iter_phase3_exact_captures(binary, descriptor))
        cuts = {row['net']:row for row in result['cut_nets']}
        captured = {}
        for row in segments:
            if row['kind'] == 'rx_to_capture':
                captured.setdefault(row['source_cut_net'], []).append(row['id'])
        fields = ('net', 'cut_class', 'source_fpgas', 'sink_fpgas', 'dependency_level',
                  'combinational_dependency_depth', 'predecessor_cut_nets')
        nodes = []
        for net, numbers in iter_phase3_exact_node_sources(binary, descriptor):
            cut = cuts.get(net)
            if cut is None or any(field not in cut for field in fields):
                raise ValidationError('behavioral oracle exact cut coverage is incomplete')
            nodes.append({**{field:cut[field] for field in fields},
                'source_segment_ids':[f'segment{number:06d}' for number in numbers],
                'capture_segment_ids':sorted(captured.get(net, []))})
        if len(nodes) != len(cuts):
            raise ValidationError('behavioral oracle exact cut coverage disagrees')
        edges = sorted(({'from':row['source_cut_net'], 'to':row['sink_cut_net'], 'segment':row['id']}
            for row in segments if row['kind'] == 'rx_to_tx'), key=lambda row:(row['to'],row['from']))
        contract = {**manifest['contract_header'], 'cut_nodes':nodes, 'logic_segments':segments,
            'capture_requirements':captures, 'dependency_edges':edges}
        if (semantic_contract_sha256(contract) != digest
                or descriptor['semantic_contract_sha256'] != digest):
            raise ValidationError('behavioral oracle exact canonical identity disagrees')
        result['semantic_contract'] = contract
    if any(native_file_generation(path) != generation for path,generation in paths.items()):
        raise ValidationError('behavioral oracle source generation changed')
    return result


if __name__ == '__main__':
    qualify(sys.argv[1],sys.argv[2])
