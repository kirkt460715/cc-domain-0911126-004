import argparse
import json
from pathlib import Path
import sys
import tempfile
from tariffs.catalog import Catalog
from tariffs.primitives import canonical
from analysis.engine import evaluate
from analysis.quality import audit_result
from analysis.groups import grouped
from publication.store import Archive
from publication.service import Service
from publication import bundle, tabular, recovery


def load(path):
    source = Path(path)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > 50_000_000:
        raise ValueError('input must be a bounded regular JSON file')
    return json.loads(source.read_text(encoding='utf-8'))


def demonstration(path, start, end):
    catalog = Catalog(load(path))
    with tempfile.TemporaryDirectory(prefix='nursery-demo-') as directory:
        root = Path(directory)
        with Archive(root / 'analysis.sqlite3') as archive:
            service = Service(archive)
            service.dispatch('input', {'id': 'county', 'document': catalog.document})
            service.dispatch('submit', {'id': 'baseline', 'input': 'county', 'start': start, 'end': end})
            service.dispatch('execute', {'id': 'baseline'})
            result = service.result('baseline')
            checked = audit_result(catalog, result, replay=True)
            if not checked['valid']:
                raise ValueError('demonstration audit failed')
            service.dispatch('release', {'id': 'baseline', 'label': 'public-v1'})
            exported = bundle.write(catalog, result, root / 'bundle')
            tabular.write_directory(catalog.document, root / 'csv')
            if tabular.read_directory(root / 'csv').fingerprint != catalog.fingerprint:
                raise ValueError('CSV roundtrip mismatch')
            saved = recovery.backup(archive, root / 'backup.sqlite3')
            recovery.restore(root / 'backup.sqlite3', root / 'restored.sqlite3', saved['digest'])
            return {
                'totals': result['totals'],
                'arithmetic_audit': checked,
                'bundle': exported,
                'archive': recovery.audit(archive),
                'by_income': grouped(result, ['income_band']),
                'data': 'local_synthetic_fixture',
            }


def parser():
    root = argparse.ArgumentParser(description='Local nursery fee and household affordability analysis')
    commands = root.add_subparsers(dest='command', required=True)
    demo = commands.add_parser('demo')
    demo.add_argument('--fixture', required=True)
    demo.add_argument('--start', default='2026-11-01')
    demo.add_argument('--end', default='2026-12-01')
    evaluate_parser = commands.add_parser('evaluate')
    evaluate_parser.add_argument('--input', required=True)
    evaluate_parser.add_argument('--start', required=True)
    evaluate_parser.add_argument('--end', required=True)
    evaluate_parser.add_argument('--endpoint', choices=('low', 'high'), default='low')
    evaluate_parser.add_argument('--missing', choices=('exclude', 'assume'), default='exclude')
    evaluate_parser.add_argument('--assumed-income', type=int)
    request = commands.add_parser('request')
    request.add_argument('--database', required=True)
    request.add_argument('--action', required=True)
    request.add_argument('--body', required=True)
    export = commands.add_parser('export')
    export.add_argument('--database', required=True)
    export.add_argument('--analysis', required=True)
    export.add_argument('--directory', required=True)
    export.add_argument('--minimum-households', type=int, default=3)
    verify = commands.add_parser('verify-bundle')
    verify.add_argument('--directory', required=True)
    csv_in = commands.add_parser('csv-input')
    csv_in.add_argument('--database', required=True)
    csv_in.add_argument('--id', required=True)
    csv_in.add_argument('--directory', required=True)
    csv_out = commands.add_parser('csv-export')
    csv_out.add_argument('--input', required=True)
    csv_out.add_argument('--directory', required=True)
    backup = commands.add_parser('backup')
    backup.add_argument('--database', required=True)
    backup.add_argument('--destination', required=True)
    restore = commands.add_parser('restore')
    restore.add_argument('--source', required=True)
    restore.add_argument('--destination', required=True)
    restore.add_argument('--checksum', required=True)
    api = commands.add_parser('serve')
    api.add_argument('--database', required=True)
    api.add_argument('--token-file', required=True)
    api.add_argument('--port', type=int, default=8080)
    return root


def execute(args):
    if args.command == 'demo':
        return demonstration(args.fixture, args.start, args.end)
    if args.command == 'evaluate':
        return evaluate(Catalog(load(args.input)), args.start, args.end, args.endpoint, args.missing, args.assumed_income)
    if args.command == 'verify-bundle':
        return bundle.verify(args.directory)
    if args.command == 'csv-export':
        return tabular.write_directory(load(args.input), args.directory)
    if args.command == 'restore':
        return recovery.restore(args.source, args.destination, args.checksum)
    if args.command == 'serve':
        from publication.http import serve
        token_path = Path(args.token_file)
        if token_path.stat().st_mode & 0o077:
            raise ValueError('token file must not be accessible to group or others')
        token = token_path.read_text(encoding='utf-8').strip()
        serve(args.database, token, port=args.port)
        return {'stopped': True}
    with Archive(args.database) as archive:
        if args.command == 'request':
            return Service(archive).dispatch(args.action, load(args.body))
        if args.command == 'export':
            service = Service(archive)
            return bundle.write(service.source(args.analysis), service.result(args.analysis), args.directory, args.minimum_households)
        if args.command == 'csv-input':
            return archive.add_input(args.id, tabular.read_directory(args.directory).document)
        if args.command == 'backup':
            return recovery.backup(archive, args.destination)
    raise ValueError('unsupported operation')


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = execute(args)
    except (ValueError, KeyError, OSError) as exc:
        print(canonical({'error': str(exc)}), file=sys.stderr)
        return 2
    print(canonical(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
