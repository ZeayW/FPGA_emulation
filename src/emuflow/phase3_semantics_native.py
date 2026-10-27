"""Bind original native ownership to its compact source and primary columns."""
from pathlib import Path
from .errors import ValidationError
from .store_publication import StoreSource, require_store_sources_unchanged


def _ownership_sources(ir,records,assignment):
    if getattr(records,'native_artifact',None) is None:return None
    source=StoreSource(Path(ir.path).resolve(),'emuir-compact-v2',records.native_source_generation)
    primary=StoreSource(Path(records.native_artifact).resolve(),'partition-cluster-columns-v2',records.native_generation)
    for prefix,kind in (('native_output',2),('native_initial',1)):
        selected=getattr(assignment,prefix+'_source',None)
        if selected is None:continue
        if (assignment.part_indices is not getattr(assignment,prefix+'_column',None)
                or assignment.part_ids!=getattr(assignment,prefix+'_parts',None)
                or assignment.native_primary_source!=primary or assignment.native_emuir_source!=source):
            raise ValidationError('native final ownership belongs to another source or column')
        refs=[source,primary,selected]
        if kind==2:refs.append(assignment.native_order_source)
        require_store_sources_unchanged(refs)
        return kind,source,primary,refs[2:]
    # One active FPGA has no graph-sized partition decision to serialize.
    if len(getattr(assignment,'part_ids',()))==1:
        return 0,source,primary,[]
    return None
