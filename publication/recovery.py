import hashlib
from pathlib import Path
import sqlite3
from tariffs.catalog import Catalog
from analysis.quality import audit_result
from publication.store import Archive


def audit(archive):
    if [row[0] for row in archive.db.execute('PRAGMA integrity_check')] != ['ok']:
        raise ValueError('SQLite integrity check failed')
    if archive.db.execute('PRAGMA foreign_key_check').fetchall():
        raise ValueError('SQLite foreign key check failed')
    inputs = [archive.input(row[0]) for row in archive.db.execute('SELECT id FROM inputs')]
    for item in inputs:
        Catalog(item['document'])
    runs = [archive.analysis(row[0]) for row in archive.db.execute('SELECT id FROM analyses')]
    for run in runs:
        source = archive.input(run['input'])
        if run['request']['input_digest'] != source['digest']:
            raise ValueError('analysis request source mismatch')
        if run['state'] == 'complete':
            if run['result'] is None or run['error'] is not None:
                raise ValueError('complete analysis has incomplete result state')
            checked = audit_result(Catalog(source['document']), run['result'], replay=True)
            if not checked['valid']:
                raise ValueError('analysis replay failed')
        elif run['result'] is not None:
            raise ValueError('unfinished analysis contains result')
        if run['state'] == 'failed' and not run['error']:
            raise ValueError('failed analysis lacks error')
    releases = [archive.publication(row[0]) for row in archive.db.execute('SELECT label FROM releases')]
    if archive.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='review_cases'").fetchone():
        from publication.review import ReviewDesk
        ReviewDesk(archive).audit()
    return {'valid': True, 'inputs': len(inputs), 'analyses': len(runs), 'releases': len(releases)}


def backup(archive, destination):
    audit(archive)
    target = Path(destination)
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch(exist_ok=False)
    output = None
    try:
        output = sqlite3.connect(str(target))
        archive.db.backup(output)
        output.close()
        output = None
        with Archive(target) as restored:
            checked = audit(restored)
    except BaseException:
        if output:
            output.close()
        target.unlink(missing_ok=True)
        raise
    return {'path': str(target), 'digest': hashlib.sha256(target.read_bytes()).hexdigest(), 'audit': checked}


def restore(source, destination, checksum):
    origin, target = Path(source), Path(destination)
    if origin.is_symlink() or not origin.is_file():
        raise ValueError('backup must be regular file')
    if hashlib.sha256(origin.read_bytes()).hexdigest() != checksum:
        raise ValueError('backup digest mismatch')
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    connection = sqlite3.connect(origin.resolve().as_uri() + '?mode=ro', uri=True)
    created = False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch(exist_ok=False)
        created = True
        output = sqlite3.connect(str(target))
        try:
            connection.backup(output)
        finally:
            output.close()
        with Archive(target) as restored:
            checked = audit(restored)
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise
    finally:
        connection.close()
    return {'path': str(target), 'audit': checked}
