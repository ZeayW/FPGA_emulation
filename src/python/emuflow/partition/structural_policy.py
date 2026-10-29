"""Finite structural policy shared by native producers and optional readers."""
import json
import fnmatch
import re
from functools import lru_cache
from emuflow.common.errors import ValidationError
from emuflow.common.paths import DATA_ROOT

STRUCTURAL_POLICY = json.loads((DATA_ROOT / 'partition/structural-policy.json').read_text())


@lru_cache(maxsize=1)
def _pattern_range_policy():
    try:
        matched = fnmatch.fnmatchcase('a', '[z-aa]')
    except re.error:
        return 'strict-ranges-v1'
    if matched:
        return 'empty-ranges-v1'
    raise ValidationError('unsupported instance pattern range convention')
