"""Shape-only protocol fixture for the retained native RouteStore seal kernel."""
import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
import subprocess
import tempfile

from emuflow.errors import ValidationError
from emuflow.native_protocol import CheckedRequestWriter
from tests.native_build import emuflow_native

_RELATIONS = {
    ('demands',): 0,
    ('routes',): 1,
    ('timing', 'paths'): 2,
    ('joint_optimization', 'candidate_generation', 'master_selection'): 3,
    ('joint_optimization', 'refinement_batches', 'batches'): 4,
}
_NAMES = ('demands', 'routes', 'timing_paths', 'master_selections', 'refinement_batch_paths')
_MAX_REQUEST = 8 * 1024 * 1024


def write_route_seal_request(path, logical):
    """Encode bounded header literals and relation references, never their rows."""
    writer = CheckedRequestWriter(path)
    nodes = 0
    def visit(value, location=()):
        nonlocal nodes
        nodes += 1
        if nodes > 65536 or len(location) > 64:
            raise ValidationError('route seal header tree exceeds bound')
        if location in _RELATIONS:
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                raise ValidationError('route seal relation is not an array')
            writer.put('BBQ', 2, _RELATIONS[location], len(value))
        elif any(key[:len(location)] == location for key in _RELATIONS) and isinstance(value, Mapping):
            if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
                raise ValidationError('route seal header object is invalid')
            writer.put('BI', 1, len(value))
            for key in sorted(value):
                writer.text(key)
                visit(value[key], (*location, key))
        else:
            writer.put('B', 0)
            literal = io.StringIO()
            encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
            for chunk in encoder.iterencode(value):
                if literal.tell() + len(chunk) > _MAX_REQUEST:
                    raise ValidationError('route seal header literal exceeds bound')
                literal.write(chunk)
            writer.text(literal.getvalue())
        if writer.stream.tell() > _MAX_REQUEST - 4:
            raise ValidationError('route seal request exceeds bound')
    try:
        writer.put('QI', 0x31515352464D5545, 1)
        visit(logical)
        writer.finish()
    finally:
        writer.close()



def seal_native_system_routes(store_path, logical):
    with tempfile.TemporaryDirectory(prefix='route-seal-fixture-') as temporary:
        request, output = Path(temporary)/'request.bin', Path(temporary)/'seal.json'
        write_route_seal_request(request, logical)
        result = subprocess.run([str(emuflow_native()), 'phase4', 'seal-routes',
            str(store_path), str(request), str(output)], capture_output=True, text=True)
        if result.returncode:
            raise ValidationError('native route seal kernel failed: '+result.stdout+result.stderr)
        return json.loads(output.read_text())
