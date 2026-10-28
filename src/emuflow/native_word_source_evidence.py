"""Publish closed source census and primitive-property exports from one DCP."""
import csv
import json
from pathlib import Path

from .errors import ValidationError
from .io import file_sha256, write_json
from .store_publication import bind_store_source


def _reference(path):
    return {'path': str(path), 'sha256': file_sha256(path)}


def publish_native_source_evidence(fpga, checkpoint, constraints, output, recipes, parents):
    """Describe raw observations; unknown sources never reject physical design."""
    census = output / 'native-source-census'
    files = {name: _reference(census / name) for name in (
        'source-census.tsv', 'external-sources.tsv', 'rx-sources.tsv', 'progress.tsv')}
    with (census / 'source-census.tsv').open(newline='') as stream:
        families = list(csv.DictReader(stream, delimiter='\t'))
    with (output / 'source-context-metrics.tsv').open(newline='') as stream:
        metrics = {row['metric']: int(row['value']) for row in csv.DictReader(stream, delimiter='\t')}
    if set(metrics) != {'requests', 'tx_targets'} or any(value < 0 for value in metrics.values()):
        raise ValidationError('native source census metrics are malformed')
    owners = [checkpoint, constraints, output / 'source-context-metrics.tsv', *recipes]
    sealed = {str(path): file_sha256(path) for path in owners}
    census_path = census / 'summary.json'
    write_json(census_path, {
        'status': 'complete-unconditional-native-capture-source-census', 'fpga': fpga,
        'capture_families': families, 'original_checkpoint_unchanged': True,
        'input_sha256': sealed, 'files': files,
        'scope': 'All native capture-family inputs and explicitly clocked original outputs',
    })
    properties_path = output / 'native-source-properties.json'
    write_json(properties_path, {
        'status': 'complete-native-source-property-observations', 'fpga': fpga,
        'requests': metrics['requests'], 'file': _reference(output / 'native-source-properties.tsv'),
        'input_sha256': {**sealed, str(census_path): file_sha256(census_path)},
        'scope': 'Unconditional native structural properties of capture and TX source unions; no numerical delay',
    })
    bundle = {'runtime_constraints': _reference(constraints), 'capture_census': _reference(census_path),
              'source_properties': _reference(properties_path)}
    bind_native_source_evidence(bundle, parents)
    return bundle


def bind_native_source_evidence(bundle, parents):
    """Keep original closed owners when reusing a routed observation."""
    if set(bundle) != {'runtime_constraints', 'capture_census', 'source_properties'}:
        raise ValidationError('native source evidence roles differ')
    references = list(bundle.values())
    for name in ('capture_census', 'source_properties'):
        receipt = Path(bundle[name]['path'])
        if file_sha256(receipt) != bundle[name]['sha256']:
            raise ValidationError('native source evidence receipt changed')
        document = json.loads(receipt.read_text())
        references.extend({'path': p, 'sha256': d} for p, d in document['input_sha256'].items())
        references.extend(document.get('files', {}).values())
        if 'file' in document:
            references.append(document['file'])
    seen = {}
    for reference in references:
        path = Path(reference['path']).resolve()
        if path in seen:
            if seen[path] != reference['sha256']:
                raise ValidationError('native source evidence has conflicting file identities')
            continue
        parents.append(bind_store_source(path, 'physical-input-document-v1'))
        if file_sha256(path) != reference['sha256']:
            raise ValidationError('native source evidence changed: ' + path.name)
        seen[path] = reference['sha256']
