"""Complete original-member source coordinates and mapped-copy owners.

The index is a source/ownership relation. Actual native endpoints, transformed
checks, optimization dispositions and numerical timing are later observations.
Phase6 admission and any normalized/original function bridge remain owned by
its caller; their existing immutable receipts can be carried as control inputs.
"""
from pathlib import Path
import json
import subprocess
import tempfile

from .errors import EmuFlowError,ValidationError
from .native_protocol import CheckedRequestWriter
from .native_tools import resolve_native_executable
from .store_publication import bind_store_source,require_store_sources_unchanged,validate_store_destination

MAGIC=0x3151454f4e4d5545
BASE_SCHEMAS=('sta-path-selector-v1','timing-path-store-v2','emuir-compact-v2','assignment-index-v4')
CONTROL_SCHEMAS={'mapped-normalization-proof-v1','emuir-compact-v2','yosys-source-document-v1','physical-input-document-v1'}


def _write_original_endpoint_request(path,*,parents,partitions,selector_sha256,source_context,control_schemas):
    writer=CheckedRequestWriter(Path(path))
    try:
        writer.put('QIII',MAGIC,1,len(partitions),len(control_schemas))
        for parent in parents:writer.put('3Q4q',*parent.generation)
        writer.text(selector_sha256);writer.text(source_context)
        for name in partitions:writer.text(name)
        for schema in control_schemas:writer.text(schema)
        writer.finish()
    finally:writer.close()


def index_original_endpoints(selector,payload,source,assignment,mapped,output,*,selector_sha256,
                             source_context,control_sources=(),source_bindings=None,executable=None):
    """Index every original ordinal, including non-state timing endpoints.

    mapped is an ordered fpga -> mapped-index mapping. No architectural object
    is protected in Vivado. Missing mapped copies remain explicit unknown
    projections, not elimination facts or zero-cost timing rows. Original IDs
    and exact endpoint spellings remain owned by the sealed selector/payload;
    members retain their ordinal in that namespace rather than copying paths.
    Existing source/function receipts retain their original generations.
    """
    if not isinstance(selector_sha256,str)or len(selector_sha256)!=64 or any(c not in'0123456789abcdef'for c in selector_sha256):
        raise ValidationError('original endpoint selector seal is invalid')
    if not isinstance(source_context,dict):raise ValidationError('original endpoint source context must be an object')
    context=json.dumps(source_context,sort_keys=True,ensure_ascii=False)
    if len(context.encode())>256*1024:raise ValidationError('original endpoint source context exceeds its bound')
    names=tuple(mapped)
    if not 0<len(names)<=64 or any(not isinstance(n,str)or not n for n in names)or names!=tuple(sorted(names)):
        raise ValidationError('original endpoint partition catalog must be canonical')
    paths=[Path(p).resolve()for p in(selector,payload,source,assignment,*mapped.values())]
    if len(set(paths))!=len(paths):raise ValidationError('original endpoint source roles alias each other')
    schemas=[*BASE_SCHEMAS,*(['xilinx-mapped-index-v2']*len(names))]
    parents=tuple(source_bindings)if source_bindings is not None else tuple(bind_store_source(p,s)for p,s in zip(paths,schemas))
    by_path={p.path:p for p in parents}
    if len(by_path)!=len(parents)or set(by_path)!=set(paths)or any(by_path[p].schema!=s for p,s in zip(paths,schemas)):
        raise ValidationError('original endpoint source generation bindings differ')
    controls=[]
    for parent in control_sources:
        if parent.schema not in CONTROL_SCHEMAS:raise ValidationError('original endpoint control source schema is unsupported')
        if parent.path in by_path:
            if by_path[parent.path]!=parent:raise ValidationError('original endpoint control source belongs to another generation')
        else:
            by_path[parent.path]=parent;controls.append(parent)
    if len(controls)>16:raise ValidationError('original endpoint control source population exceeds its bound')
    ordered=tuple(by_path[p]for p in paths)+tuple(controls)
    output=Path(output).resolve();validate_store_destination(output,ordered);output.parent.mkdir(parents=True,exist_ok=True)
    native=resolve_native_executable('emuflow-native',executable)
    with tempfile.TemporaryDirectory(prefix='.original-endpoint-index-',dir=output.parent)as directory:
        request=Path(directory)/'request.bin'
        _write_original_endpoint_request(request,parents=ordered,partitions=names,selector_sha256=selector_sha256,
            source_context=context,control_schemas=[p.schema for p in controls])
        argv=[native,'phase7','index-original-endpoints',*(str(p)for p in paths[:4]),str(request),str(output),
            *(str(p)for p in paths[4:]),*(str(p.path)for p in controls)]
        result=subprocess.run(argv,capture_output=True,text=True)
        if result.returncode:raise EmuFlowError('original endpoint indexing failed: '+result.stderr[-16384:])
    require_store_sources_unchanged(ordered)
    return output
