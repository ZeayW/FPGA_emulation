"""Bounded optional API for the shared native BoardDB authority."""
import json
import os
from pathlib import Path
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation, resolve_native_executable
from emuflow.common.tool_runtime import NativeToolProcess

_FIELDS=('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')
_LIMIT=8*1024*1024


def normalize_board(value=None, *, path=None):
    """Return finite normalized board configuration, never circuit records."""
    with tempfile.TemporaryDirectory(prefix='.board-configuration-') as temporary:
        root=Path(temporary).resolve()
        if path is None:
            source=root/'board.json'
            try:
                text=json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False)
            except (TypeError,ValueError) as error:
                raise ValidationError('invalid BoardDB configuration: '+str(error)) from error
            if len(text.encode())>_LIMIT:raise ValidationError('BoardDB configuration exceeds bound')
            source.write_text(text,encoding='utf-8')
        else:
            source=Path(path).resolve()
        generation=native_file_generation(source)
        native=resolve_native_executable('emuflow-native')
        output,proof=root/'normalized.json',root/'operation.json'
        with NativeToolProcess(native,[native,'--report',str(proof),'board','normalize',str(source),str(output)],
                environment=dict(os.environ),scratch_parent=root) as process:
            completed=process.finish()
        if completed.returncode:
            message=completed.stdout
            if proof.is_file() and proof.stat().st_size<=256*1024:
                try: message=json.loads(proof.read_text(encoding='utf-8'))['error']['message']
                except (ValueError,KeyError,TypeError): pass
            raise ValidationError('native BoardDB admission failed: '+message)
        if output.stat().st_size>_LIMIT or proof.stat().st_size>256*1024:
            raise ValidationError('native BoardDB result exceeds bound')
        operation=json.loads(proof.read_text(encoding='utf-8'))
        if (operation.get('schema')!='emuflow.native-stage-result/v1'
                or operation.get('stage')!='board.normalize' or operation.get('status')!='pass'
                or len(operation.get('inputs',[]))!=1 or len(operation.get('outputs',[]))!=1):
            raise ValidationError('native BoardDB producer identity disagrees')
        def receipt(row,target,expected):
            if (row.get('schema')!='boarddb-v1' or row.get('path')!=str(target)
                    or tuple(row.get('generation',{}).get(k) for k in _FIELDS)!=expected
                    or native_file_generation(target)!=expected):
                raise ValidationError('native BoardDB source/output generation changed')
        published=native_file_generation(output)
        receipt(operation['inputs'][0],source,generation)
        receipt(operation['outputs'][0],output,published)
        result=json.loads(output.read_text(encoding='utf-8'))
        if result.get('schema')!='emuflow.boarddb/v1':raise ValidationError('native BoardDB version disagrees')
        receipt(operation['inputs'][0],source,generation)
        receipt(operation['outputs'][0],output,published)
        if path is not None and Path(path).resolve()!=source:
            raise ValidationError('native BoardDB source alias changed')
        return result
