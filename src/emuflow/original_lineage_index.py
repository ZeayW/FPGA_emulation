"""Complete original source-copy/native-object observation ledger.

Numerical timing uses the actual native graph. This independent ledger retains
all original member IDs through their source index without forcing old names,
primitive types or gating LUTs to survive optimization.
"""
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import tempfile

from .errors import EmuFlowError, ValidationError
from .native_protocol import CheckedRequestWriter
from .native_tools import resolve_native_executable
from .store_publication import bind_store_source, require_store_sources_unchanged, validate_store_destination

MAGIC=0x31514c4f4e4d5545


def _write_original_lineage_request(path, *, parents, partitions, stage):
    writer=CheckedRequestWriter(Path(path))
    try:
        writer.put('QII',MAGIC,1,len(partitions))
        for parent in parents: writer.put('3Q4q',*parent.generation)
        writer.text(stage)
        for name in partitions: writer.text(name)
        writer.finish()
    finally: writer.close()


def index_original_lineage(endpoint_index, partitions, output, *, stage='routed', source_bindings=None, executable=None):
    """Join every original source copy to all observed native tagged objects.

    partitions maps each FPGA name to its immutable (mapped, lineage) stores.
    Missing tags, changed types and multiple descendants remain explicit facts;
    they do not stop normal Vivado optimization or receive fake timing values.
    Pin/check associations and global numerical timing remain separate inputs.
    """
    if stage not in ('synthesized','optimized','routed'):
        raise ValidationError('original lineage observation stage is invalid')
    endpoint_index=Path(endpoint_index).resolve()
    with closing(sqlite3.connect(endpoint_index.as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
        names=tuple(row[0] for row in db.execute('SELECT fpga FROM parts ORDER BY part_index'))
    if not 0<len(names)<=64 or set(partitions)!=set(names):
        raise ValidationError('original lineage partition coverage differs')
    paths=[endpoint_index]
    for name in names:
        pair=partitions[name]
        if not isinstance(pair,(tuple,list)) or len(pair)!=2:
            raise ValidationError('original lineage partition source roles differ')
        paths.extend(Path(p).resolve() for p in pair)
    schemas=['native-original-endpoint-index-v1',*(['xilinx-mapped-index-v2','vivado-lineage-store-v2']*len(names))]
    parents=tuple(source_bindings) if source_bindings is not None else tuple(bind_store_source(p,s) for p,s in zip(paths,schemas))
    by_path={p.path:p for p in parents}
    if len(by_path)!=len(parents) or len(set(paths))!=len(paths) or set(by_path)!=set(paths) or any(by_path[p].schema!=s for p,s in zip(paths,schemas)):
        raise ValidationError('original lineage source generation bindings differ')
    ordered=tuple(by_path[p] for p in paths)
    output=Path(output).resolve();validate_store_destination(output,ordered);output.parent.mkdir(parents=True,exist_ok=True)
    native=resolve_native_executable('emuflow-native',executable)
    with tempfile.TemporaryDirectory(prefix='.original-lineage-',dir=output.parent) as temporary:
        request=Path(temporary)/'request.bin'
        _write_original_lineage_request(request,parents=ordered,partitions=names,stage=stage)
        result=subprocess.run([native,'phase7','index-original-lineage',str(endpoint_index),str(request),str(output),
                               *(str(p) for p in paths[1:])],capture_output=True,text=True)
        if result.returncode:
            raise EmuFlowError('original lineage indexing failed: '+result.stderr[-16384:])
    require_store_sources_unchanged(ordered)
    return output
