"""Bounded control of full-source planning searches in one OpenSTA context.

The native engine owns graph propagation, independent metric searches and
setup statistics. Python handles at most 64 checkpoint descriptors and result
rows; path witnesses remain a streamed artifact for the native consumer.
These predicted communication costs are not routed timing or safe frequency.
"""
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import tempfile

from .errors import EmuFlowError, ValidationError
from .io import file_sha256, write_json
from .native_tools import resolve_native_executable
from .opensta import OPENSTA_EXPORT_SCRIPT
from .opensta_source_context import OpenSTASourceContext
from .store_publication import (VerifiedStoreFile, bind_store_source,
    StoreSource, require_store_sources_unchanged, validate_store_destination)
from .tool_runtime import NativeToolProcess


@dataclass(frozen=True)
class PatronSourceCheckpoint:
    identity: str
    profile: VerifiedStoreFile


def check_patron_source_assignments(model, context, assignments, output, *, timeout_seconds=3600):
    """Join native complete-connectivity costs to the original source graph.

    Each item is (identity, existing packed assignment). The shared native
    producer owns the pin/ownership/route join, including crossings absent
    from the selected timing database. Retain profiles and packed requests
    beside the engine result so a checkpoint has reviewable input evidence.
    """
    from .phase3_pressure_native import NativePressureModel
    if (not isinstance(model, NativePressureModel) or not isinstance(context, OpenSTASourceContext)
            or not isinstance(assignments, (list, tuple)) or not 1 <= len(assignments) <= 64
            or any(not isinstance(item, (list, tuple)) or len(item) != 2 for item in assignments)):
        raise ValidationError('invalid native PATRON source assignment checkpoints')
    source = context.source_bindings[0]
    if source.path != model.ir.path.resolve() or source.generation != model.generations[0]:
        raise ValidationError('PATRON assignment and timing context belong to different original sources')
    primary = StoreSource(Path(model.primary_artifact).resolve(), 'partition-cluster-columns-v2', model.primary_generation)
    output = Path(output).resolve()
    validate_store_destination(output, (*context.source_bindings, primary))
    if output.exists():
        raise ValidationError('PATRON source assignment destination already exists')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.patron-source-assignments-', dir=output.parent) as temporary:
        root = Path(temporary)
        checkpoints, profiles = [], []
        for index, (identity, assignment) in enumerate(assignments):
            directory = root/'profiles'/f'{index:04d}'
            directory.mkdir(parents=True)
            path = directory/'source-proxy.tsv'
            report = model.export_source_proxy(directory, path, assignment)
            checkpoints.append(PatronSourceCheckpoint(identity, VerifiedStoreFile(path, 'patron-source-proxy-tsv-v1')))
            profiles.append(dict(checkpoint=identity, path=path.relative_to(root).as_posix(), producer=report))
        result = evaluate_patron_source_checkpoints(context, checkpoints, root/'evaluation',
            top=model.ir.value['design']['top'], timeout_seconds=timeout_seconds)
        result = {**result, 'profiles': profiles,
            'witnesses': {**result['witnesses'], 'path':'evaluation/witnesses.tsv'}}
        # Producer diagnostics contain temporary absolute paths. Artifact
        # references use the retained relative profile above after publication.
        for profile in profiles:
            profile['producer'].pop('output', None)
        write_json(root/'summary.json', result)
        require_store_sources_unchanged((*context.source_bindings, primary))
        os.rename(root, output)
    return result


def evaluate_patron_source_checkpoints(context, checkpoints, output, *, top,
                                      timeout_seconds=3600):
    """Search every supplied assignment profile without reloading the graph.

    Profiles come from the shared native complete-connectivity producer.
    The caller retains its original source context and generation leases.
    H and sum(maximum route R per crossing net) are independent maxima,
    followed by standard engine setup worst slack/TNS using predicted delays.
    This does not reuse the v14 sampled-path objective or certify its search.
    """
    if (not isinstance(context, OpenSTASourceContext)
            or not isinstance(checkpoints, (list, tuple)) or not 1 <= len(checkpoints) <= 64
            or not isinstance(top, str) or not top or '\0' in top
            or isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int)
            or timeout_seconds <= 0 or not 1 <= len(context.reference_clocks_ns) <= 64):
        raise ValidationError('invalid bounded PATRON source checkpoint request')
    labels = []
    for item in checkpoints:
        if (not isinstance(item, PatronSourceCheckpoint)
                or not isinstance(item.identity, str) or not 1 <= len(item.identity.encode()) <= 128
                or '\0' in item.identity or item.identity in labels
                or not isinstance(item.profile, VerifiedStoreFile)
                or item.profile.source.schema != 'patron-source-proxy-tsv-v1'):
            raise ValidationError('invalid or duplicate PATRON source checkpoint')
        labels.append(item.identity)
    context.require_unchanged()
    scripts = tuple(bind_store_source(OPENSTA_EXPORT_SCRIPT.with_name(name), 'sta-source-document-v1')
        for name in ('patron_source_checkpoints.tcl', 'communication_delays.tcl', 'design_context.tcl'))
    parents = (*context.source_bindings, *scripts, *(item.profile.source for item in checkpoints))
    output = Path(output).resolve()
    validate_store_destination(output, parents)
    if output.exists():
        raise ValidationError('PATRON source checkpoint destination already exists')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.patron-source-checkpoints-', dir=output.parent) as temporary:
        root = Path(temporary)
        request, metrics, witnesses = (root/name for name in ('requests.tsv', 'metrics.tsv', 'witnesses.tsv'))
        request.write_text('checkpoint_hex\tprofile_path_hex\n' + ''.join(
            f'{item.identity.encode().hex()}\t{str(item.profile.source.path).encode().hex()}\n'
            for item in checkpoints))
        environment = {**os.environ, **context.environment, 'EMUFLOW_STA_TOP': top,
            'EMUFLOW_PATRON_CHECKPOINTS': str(request), 'EMUFLOW_PATRON_METRICS': str(metrics),
            'EMUFLOW_PATRON_WITNESSES': str(witnesses)}
        command = [context.command[0], '-no_init', '-exit', str(scripts[0].path)]
        with NativeToolProcess(resolve_native_executable('emuflow-native'), command,
                environment=environment, timeout_ms=timeout_seconds*1000,
                scratch_parent=root, maximum_bytes=65536) as process:
            completed = process.finish()
        require_store_sources_unchanged(parents)
        completion = re.findall(r'^EMUFLOW_PATRON_SOURCE_CHECKPOINTS status=pass contexts=1 checkpoints=(\d+)\r?$',
                                completed.stdout, re.M)
        if completed.returncode or completion != [str(len(checkpoints))]:
            raise EmuFlowError('PATRON full-source checkpoint search failed: '+completed.stdout)
        if not metrics.is_file() or metrics.stat().st_size > 65536 or not witnesses.is_file():
            raise EmuFlowError('PATRON full-source checkpoint output is incomplete or unbounded')
        lines = metrics.read_text().splitlines()
        if (not lines or lines[0] != 'checkpoint_hex\tmaximum_physical_hops\tmaximum_path_route_ratio_sum\tworst_slack_ns\ttotal_negative_slack_ns\twitnesses'
                or len(lines) != len(checkpoints)+1):
            raise EmuFlowError('PATRON full-source checkpoint metrics header/count differs')
        rows = []
        for item, line in zip(checkpoints, lines[1:]):
            fields = line.split('\t')
            if len(fields) != 6 or fields[0] != item.identity.encode().hex():
                raise EmuFlowError('PATRON full-source checkpoint identity differs')
            try:
                hops, ratios, count = (int(fields[i]) for i in (1, 2, 5))
                slack, tns = (float(fields[i]) for i in (3, 4))
            except ValueError as error:
                raise EmuFlowError('PATRON full-source checkpoint metrics are invalid') from error
            if (not 0 <= hops < 8388608 or not 0 <= ratios < 8388608
                    or not 3 <= count <= 3*(len(context.reference_clocks_ns)+2)
                    or not math.isfinite(slack) or not math.isfinite(tns) or tns > 0):
                raise EmuFlowError('PATRON full-source checkpoint metrics exceed their contract')
            rows.append(dict(checkpoint=item.identity, maximum_physical_hops=hops,
                maximum_path_route_ratio_sum=ratios, worst_slack_ns=slack,
                total_negative_slack_ns=tns, witnesses=count, profile_sha256=item.profile.sha256))
        result = dict(schema='emuflow.patron-source-checkpoints/v1', status='pass',
            scope='partition-planning-full-source-checkpoints', engine_contexts=1,
            objective_policy='independent-source-hops-route-ratio-and-setup-sta-v1',
            reference_clocks_ns=context.reference_clocks_ns,
            checkpoints=rows, source_context_inputs={str(p):v for p,v in context.timing_inputs.items()},
            adapter_scripts={str(p.path):file_sha256(p.path) for p in scripts},
            witnesses=dict(path='witnesses.tsv', sha256=file_sha256(witnesses)),
            engine_metrics=completed.native_metrics)
        write_json(root/'summary.json', result)
        require_store_sources_unchanged(parents)
        os.rename(root, output)
    return result
