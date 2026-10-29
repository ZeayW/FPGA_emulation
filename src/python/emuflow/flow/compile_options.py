"""Finite compile defaults shared with the native admission owner."""
import json
from pathlib import Path
import sys

from emuflow.common.paths import DATA_ROOT, PACKAGE_ROOT, REPOSITORY_ROOT

CONTRACT_PATH = DATA_ROOT / 'flow/compile-options.json'


def _resource(value):
    relative=Path(value['path'])
    if value['scope']=='package':return PACKAGE_ROOT/relative
    candidates=(REPOSITORY_ROOT/relative,REPOSITORY_ROOT/'share/emuflow'/relative,
                Path(sys.prefix)/'share/emuflow'/relative)
    return next((p for p in candidates if p.is_file()),candidates[0])


_CONTRACT=json.loads(CONTRACT_PATH.read_text(encoding='utf-8'))
if _CONTRACT.get('schema')!='emuflow.compile-options/v1':
    raise RuntimeError('unsupported compile options contract')
COMPILE_DEFAULTS={key:(_resource(entry['resource']) if 'resource' in entry else
                     tuple(entry['default']) if entry['kind'].endswith('-list') else entry['default'])
                  for key,entry in _CONTRACT['options'].items()}
