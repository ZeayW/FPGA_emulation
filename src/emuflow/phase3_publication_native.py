"""Bounded native final-directory transaction for ordinary Phase 3 production."""
import json
import os
from pathlib import Path
import tempfile

from .errors import ValidationError
from .native_protocol import CheckedRequestWriter
from .native_tools import require_native_schema, resolve_native_executable
from .store_publication import StoreSource, bind_store_source, require_store_sources_unchanged
from .tool_runtime import NativeToolProcess


def publish(output, ir, clusters, assignment, report):
    from .phase3_assignment_native import native_assignment_cut_source
    from .phase3_cluster_catalog import catalog_sources, write_catalog_request
    require_native_schema('partition-publication-request-v1')
    cut = native_assignment_cut_source(assignment)
    if cut is None or cut.ownership is None:
        raise ValidationError('native partition publication requires original selected ownership')
    records = clusters['clusters'];policy = clusters['policy'];eligible = policy.get('eligible_combinational_cut_nets', [])
    catalog = catalog_sources(records, eligible)
    if len(catalog) != 3 or catalog[:2] != cut.ownership.sources[:2]:
        raise ValidationError('native partition publication candidate/source ownership disagrees')
    if cut.auxiliary_sources[0].schema != 'partition-cut-counts-v1':
        raise ValidationError('native partition publication requires original endpoint counts')
    output = Path(output);output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValidationError('native partition publication requires an absent final directory')
    contract = assignment['semantic_contract']
    header = {key:value for key,value in contract.items() if key not in {
        'cut_nodes','dependency_edges','logic_segments','capture_requirements'}}
    metadata = {'schema':'emuflow.phase3-assignment-sqlite/v4','design':assignment['design'],
        'platform':assignment['platform'],'provider':assignment['provider'],'seed':assignment['seed'],
        'source_instance_count':len(assignment['instance_assignment']),'constraints':assignment['constraints'],
        'semantic_contract_sha256':assignment['semantic_contract_sha256'],'semantic_contract_schema':contract['schema'],
        'semantic_contract_header':header,'semantic_contract_binary':None}
    config = {'index_metadata':metadata,
        'document':{key:value for key,value in assignment.items() if key not in {
            'cluster_assignment','instance_assignment','partitions','semantic_contract','cut_nets'}},
        'partitions':[{key:value for key,value in part.items() if key!='clusters'} for part in assignment['partitions']],
        'report':report}
    encoded = json.dumps(config,sort_keys=True,separators=(',', ':'),allow_nan=False)
    if len(encoded.encode()) > 8*1024*1024-4096:
        raise ValidationError('native partition publication header exceeds bound')
    with tempfile.TemporaryDirectory(prefix='.phase3-publication-',dir=output.parent) as temporary:
        root=Path(temporary);catalog_request=root/'catalog.bin';request=root/'request.bin';operation=root/'operation.json'
        document={key:value for key,value in clusters.items() if key not in {'clusters','policy'}}
        document['policy']={key:value for key,value in policy.items() if key!='eligible_combinational_cut_nets'}
        write_catalog_request(catalog_request,clusters,document,eligible,'eligible_combinational_cut_nets' in policy,catalog)
        sources=(catalog[0],catalog[1],
            bind_store_source(cut.paths[0],'exact-nodes-v2'),bind_store_source(cut.paths[1],'exact-captures-v1'),
            cut.auxiliary_sources[0],bind_store_source(catalog_request,'cluster-catalog-request-v1'),catalog[2],*cut.ownership.sources[2:])
        if tuple(item.generation for item in sources[2:4]) != cut.generations:
            raise ValidationError('native partition publication semantic generation changed')
        require_store_sources_unchanged(sources)
        writer=CheckedRequestWriter(request)
        try:
            writer.put('QII',0x31555050464D5545,1,len(cut.ownership.sources)-2)
            for item in sources:writer.put('3Q4q',*item.generation)
            writer.catalog(sorted(part['fpga'] for part in assignment['partitions']))
            writer.catalog(cut.ownership.parts);writer.text(encoded);writer.finish()
        finally:writer.close()
        executable=resolve_native_executable('emuflow-native')
        with NativeToolProcess(executable,[executable,'--report',str(operation),'phase3','publish-partition',
                *(str(item.path) for item in sources),str(request),str(output)],environment=dict(os.environ),
                timeout_ms=21600000,scratch_parent=root) as process:
            completed=process.finish()
        if completed.returncode:raise ValidationError('native partition publication failed: '+completed.stdout)
        require_store_sources_unchanged(sources)
        if operation.stat().st_size>256*1024:raise ValidationError('native partition result exceeds bound')
        proof=json.loads(operation.read_text())
        if proof.get('stage')!='phase3.publish-partition' or proof.get('status')!='pass' or len(proof.get('outputs',[]))!=4:
            raise ValidationError('native partition producer result disagrees')
        fields=('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')
        originals=[]
        for record,name,schema in zip(proof['outputs'],
                ('phase3_report.json','assignment.sqlite','assignment.exact.bin','assignment.json'),
                ('phase3-report-v1','assignment-index-v4','phase3-exact-data-v2','assignment-selector-v4')):
            path=(output/name).resolve();generation=record.get('generation',{})
            if (record.get('path')!=str(path) or record.get('schema')!=schema
                    or any(type(generation.get(key)) is not int for key in fields)):
                raise ValidationError('native partition original output binding disagrees')
            originals.append(StoreSource(path,schema,tuple(generation[key] for key in fields)))
        require_store_sources_unchanged(originals)
        if (output/'phase3_report.json').stat().st_size>8*1024*1024:
            raise ValidationError('native partition report exceeds bound')
        result=json.loads((output/'phase3_report.json').read_text())
        if result.get('finalization_implementation_id')!='native-partition-publication-v1':
            raise ValidationError('native partition implementation identity disagrees')
        return result
