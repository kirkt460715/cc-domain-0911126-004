import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from tariffs.catalog import Catalog
from tariffs.primitives import canonical
from analysis.engine import evaluate
from analysis.quality import audit_result
from analysis.groups import grouped, income_missingness
from analysis.providers import provider_report, subsidy_incidence
from publication.disclosure import publication_packet, validate_packet


def contents(catalog, result, threshold):
    checked = audit_result(catalog, result, replay=True)
    if not checked['valid']:
        raise ValueError('cannot export inconsistent analysis')
    packet = publication_packet(result, threshold)
    validate_packet(packet)
    files = {
        'input.json': canonical(catalog.document),
        'analysis.json': canonical(result),
        'audit.json': canonical(checked),
        'public.json': canonical(packet),
        'by-region.json': canonical(grouped(result, ['region'])),
        'by-income.json': canonical(grouped(result, ['income_band'])),
        'providers.json': canonical(provider_report(catalog, result)),
        'incidence.json': canonical(subsidy_incidence(catalog, result)),
        'missingness.json': canonical(income_missingness(result)),
    }
    manifest = {
        'version': 1,
        'access': 'restricted_analysis_contains_synthetic_or_authorized_household_data',
        'input_digest': catalog.fingerprint,
        'result_digest': result['result_digest'],
        'minimum_households': threshold,
        'files': {name: hashlib.sha256(value.encode()).hexdigest() for name, value in sorted(files.items())},
    }
    files['manifest.json'] = canonical(manifest)
    return files


def write(catalog, result, directory, threshold=3):
    files = contents(catalog, result, threshold)
    root = Path(directory)
    if root.exists() or root.is_symlink():
        raise FileExistsError(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.nursery-', dir=root.parent))
    try:
        for name, content in files.items():
            with (temporary / name).open('w', encoding='utf-8', newline='') as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        os.rename(temporary, root)
    except BaseException:
        shutil.rmtree(temporary)
        raise
    return verify(root)


def verify(directory):
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('bundle must be regular directory')
    path = root / 'manifest.json'
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 100000:
        raise ValueError('invalid manifest')
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if manifest.get('version') != 1 or not isinstance(manifest.get('files'), dict):
        raise ValueError('unsupported manifest')
    names = set(manifest['files']) | {'manifest.json'}
    if names != {path.name for path in root.iterdir()}:
        raise ValueError('bundle file set mismatch')
    for name, checksum in manifest['files'].items():
        if Path(name).name != name or name in ('.', '..'):
            raise ValueError('unsafe bundle filename')
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 100_000_000:
            raise ValueError('expected bounded regular bundle file')
        if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            raise ValueError('bundle checksum mismatch')
    catalog = Catalog(json.loads((root / 'input.json').read_text(encoding='utf-8')))
    result = json.loads((root / 'analysis.json').read_text(encoding='utf-8'))
    expected = contents(catalog, result, manifest['minimum_households'])
    for name, value in expected.items():
        if (root / name).read_bytes() != value.encode():
            raise ValueError('derived bundle content mismatch')
    return {'verified': True, 'result_digest': result['result_digest'], 'files': len(names)}
