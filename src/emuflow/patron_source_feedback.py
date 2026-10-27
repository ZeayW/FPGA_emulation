"""Bounded PATRON proposal rounds with complete-source acceptance.

Native code merges nominal witnesses and searches candidate ownership. The
source OpenSTA graph, rather than the proposal path population, scores every
candidate. Checkpoint models remain planning estimates, not physical timing.
"""
import json
import math
import os
from pathlib import Path
import tempfile

from .errors import EmuFlowError, ValidationError
from .indexed_timing import IndexedTimingView
from .io import read_json, write_json
from .native_protocol import CheckedRequestWriter
from .native_tools import resolve_native_executable
from .patron_source_checkpoints import check_patron_source_assignments
from .sta import import_sta_path_database_tsv_streaming
from .store_publication import bind_store_source, require_store_sources_unchanged, validate_store_destination
from .tool_runtime import NativeToolProcess


def merge_source_witnesses(context, checkpoints, output, *, executable=None):
    """Merge the closed nominal TSVs with shared native identity/dedup logic."""
    if not isinstance(checkpoints,(list,tuple)) or not 1 <= len(checkpoints) <= 9:
        raise ValidationError('PATRON source witness owner count is invalid')
    context.require_unchanged()
    source=context.source_bindings[0]
    raw=tuple(bind_store_source(Path(p)/'evaluation/feedback.tsv','sta-source-document-v1') for p in checkpoints)
    parents=(source,*raw)
    output=Path(output).resolve();validate_store_destination(output,(*context.source_bindings,*raw))
    if output.exists():raise ValidationError('source witness destination already exists')
    output.parent.mkdir(parents=True,exist_ok=True)
    native=resolve_native_executable('emuflow-native',executable)
    with tempfile.TemporaryDirectory(prefix='.patron-witnesses-',dir=output.parent) as temporary:
        root=Path(temporary);request=root/'request.bin'
        writer=CheckedRequestWriter(request)
        try:
            writer.put('QII',0x31514d57544d5545,1,len(parents))
            for parent in parents:writer.put('3Q4q',*parent.generation)
            writer.finish()
        except BaseException:
            writer.close();raise
        command=[native,'phase3','merge-witness-paths',str(source.path),str(request),str(root/'paths.tsv'),
                 *(str(parent.path) for parent in raw)]
        with NativeToolProcess(native,command,scratch_parent=root,maximum_bytes=65536) as process:
            completed=process.finish()
        if completed.returncode:raise EmuFlowError('native source witness merge failed: '+completed.stdout)
        try:summary=json.loads(completed.stdout)
        except ValueError as error:raise EmuFlowError('source witness merge lacks its summary')from error
        if (summary.get('schema')!='emuflow.patron-witness-merge/v1' or summary.get('status')!='pass'
                or type(summary.get('paths'))is not int or summary['paths']<=0):
            raise EmuFlowError('source witness merge summary is invalid')
        require_store_sources_unchanged((*context.source_bindings,*raw))
        view=IndexedTimingView(source.path)
        try:
            import_sta_path_database_tsv_streaming(root/'paths.tsv',source.path,root/'paths.json',
                provider='opensta-fpga-path-database-v1',source=dict(clock_budget_policy='engine-edge-v1',
                    clock_logic_policy=context.model['clock_logic']['policy'],
                    path_selection=dict(policy='accumulated-source-witnesses-v1',scope='selected-witnesses')),
                _ir=view,expected_records=summary['paths'])
        finally:view.close()
        write_json(root/'summary.json',summary)
        require_store_sources_unchanged((*context.source_bindings,*raw));os.rename(root,output)
    return summary


def source_checkpoint_rank(metrics):
    """Versioned global objective; retain raw engine times in the report."""
    hops,ratios=metrics.get('maximum_physical_hops'),metrics.get('maximum_path_route_ratio_sum')
    times=[metrics.get('worst_slack_ns'),metrics.get('total_negative_slack_ns')]
    if (type(hops)is not int or type(ratios)is not int or hops<0 or ratios<0
            or any(isinstance(v,bool)or not isinstance(v,(int,float))or not math.isfinite(v)for v in times)
            or times[1]>0):raise ValidationError('complete-source objective is invalid')
    return (hops,ratios,*(round(-value/1.0e-6)for value in times))


def run_patron_source_feedback(model,context,clusters,initial,output,*,max_moves=None,
                               timeout_seconds=3600,max_rounds=4,executable=None):
    """Propose on accumulated witnesses; accept only a full-graph improvement.

    Rejected candidates contribute newly exposed paths to subsequent rounds.
    A bounded search can retain the initializer; the final graph result is
    still complete, without claiming global partition optimality.
    """
    from .partition import validate_partition_artifacts_online
    from .partition_pressure import build_partition_pressure_model,run_partition_pressure_native
    if type(max_rounds)is not int or not 1 <= max_rounds <= 8:
        raise ValidationError('PATRON source feedback rounds must be in [1,8]')
    root=Path(output).resolve();root.mkdir(parents=True,exist_ok=False)
    baseline=check_patron_source_assignments(model,context,[('initial',initial)],root/'initial',timeout_seconds=timeout_seconds)
    best=initial;best_metrics=baseline['checkpoints'][0];initial_rank=source_checkpoint_rank(best_metrics)
    owners=[root/'initial'];rounds=[];accepted=0;termination='round-limit';previous_paths=None
    for number in range(max_rounds):
        bank=root/f'witnesses-{number:02d}'
        merged=merge_source_witnesses(context,owners,bank,executable=executable)
        proposal_model=build_partition_pressure_model(model.ir,model.platform,clusters,model.constraints,
            read_json(bank/'paths.json'),model.routes,independent_validation=False,
            timing_database_path=bank/'paths.json',native_store_model=True)
        candidate,trace=run_partition_pressure_native(model.ir,model.platform,clusters,model.constraints,model.routes,
            proposal_model,best,max_moves=max_moves,algorithm_version=14,executable=executable,
            retain_trace_seals=False,stream_semantic_records=True,output_validation='caller',
            timeout_seconds=timeout_seconds,_release_model_before_assignment=True,_defer_candidate_derived_records=True)
        validate_partition_artifacts_online(model.platform,clusters,candidate)
        record=dict(round=number,witness_paths=merged['paths'],proposal_model=trace['model_counts'],accepted=False)
        if candidate['cluster_assignment']==best['cluster_assignment']:
            record['reason']='unchanged-proposal';rounds.append(record);termination='unchanged-proposal';break
        checkpoint=root/f'candidate-{number:02d}'
        evaluated=check_patron_source_assignments(model,context,[(f'candidate-{number}',candidate)],checkpoint,
            timeout_seconds=timeout_seconds)
        metrics=evaluated['checkpoints'][0];rank=source_checkpoint_rank(metrics)
        record.update(rank=list(rank),metrics=metrics,checkpoint=checkpoint.name,
                      proposal_trace=dict(move_count=trace['move_count'],batch_count=trace['batch_count']))
        owners.append(checkpoint)
        if rank<source_checkpoint_rank(best_metrics):
            best,best_metrics=candidate,metrics;accepted+=1;record['accepted']=True
        elif previous_paths==merged['paths']:
            termination='no-new-critical-witnesses';rounds.append(record);break
        previous_paths=merged['paths'];rounds.append(record)
    final=dict(best,provider='patron-v15-source-feedback-v1',provider_metadata={
        'initial_provider':initial.get('provider'),'candidate_selection':'complete-source-checkpoint-acceptance-v1'})
    selection=dict(status='pass',provider='patron-source-objective-selection-v1',
        policy='complete-source-hop-ratio-setup-v1',objective_fields=['maximum_physical_hops',
            'maximum_path_route_ratio_sum','negative_worst_setup_slack_ns','negative_setup_tns_ns'],
        time_rank_quantum_ns=1.0e-6,initial_timing_rank=list(initial_rank),
        candidate_timing_rank=list(source_checkpoint_rank(best_metrics)),selected='candidate'if accepted else'initial',
        assignment_changed=bool(accepted),accepted_rounds=accepted)
    report=dict(schema='emuflow.patron-source-feedback/v1',status='pass',algorithm_version=15,
        termination=termination,max_rounds=max_rounds,rounds=rounds,initial_metrics=baseline['checkpoints'][0],
        final_metrics=best_metrics,candidate_selection=selection,scope='complete-source-planning-timing')
    context.require_unchanged();write_json(root/'report.json',report)
    return final,report
