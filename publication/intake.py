import copy
from tariffs.catalog import Catalog
from tariffs.primitives import fields, digest, word
from publication.tabular import SCHEMAS, read_table


def key_for(table, row):
    if table == 'fees':
        return row['provider'], row['id'], row['start']
    return (row['id'],)


def apply_patch(catalog, operations):
    if not isinstance(operations, list) or len(operations) > 10000:
        raise ValueError('patch requires bounded operation list')
    document = catalog.export()
    touched = set()
    changes = []
    for operation in operations:
        fields(operation, ('table', 'action', 'key'), ('row',))
        table = operation['table']
        action = operation['action']
        if table not in SCHEMAS or action not in ('add', 'replace', 'remove'):
            raise ValueError('unsupported patch target or action')
        key = operation['key']
        width = 3 if table == 'fees' else 1
        if not isinstance(key, list) or len(key) != width:
            raise ValueError('incorrect patch key width')
        for part in key:
            word(part, 'patch.key')
        key = tuple(key)
        target = table, key
        if target in touched:
            raise ValueError('a patch may touch a record only once')
        touched.add(target)
        matches = [index for index, row in enumerate(document[table]) if key_for(table, row) == key]
        if action == 'add' and matches:
            raise ValueError('patch add target already exists')
        if action != 'add' and len(matches) != 1:
            raise ValueError('patch requires exactly one existing record')
        previous = document[table][matches[0]] if matches else None
        if action == 'remove':
            if 'row' in operation:
                raise ValueError('remove must not contain a replacement row')
            del document[table][matches[0]]
            following = None
        else:
            if 'row' not in operation:
                raise ValueError('add or replace requires row')
            following = copy.deepcopy(operation['row'])
            fields(following, SCHEMAS[table])
            if key_for(table, following) != key:
                raise ValueError('replacement record key mismatch')
            if action == 'add':
                document[table].append(following)
            else:
                document[table][matches[0]] = following
        changes.append({'table': table, 'key': list(key), 'action': action, 'before_digest': digest(previous), 'after_digest': digest(following)})
    revised = Catalog(document)
    return revised, {'before_digest': catalog.fingerprint, 'after_digest': revised.fingerprint, 'changes': changes, 'atomic_validation': True}


def merge_tables(catalog, tables, mode='replace'):
    if mode not in ('replace', 'append'):
        raise ValueError('table import mode must be replace or append')
    if not isinstance(tables, dict) or not tables or tables.keys() - SCHEMAS.keys():
        raise ValueError('unknown or empty table selection')
    document = catalog.export()
    counts = {}
    for name, text in tables.items():
        rows = read_table(text, name)
        counts[name] = len(rows)
        document[name] = rows if mode == 'replace' else document[name] + rows
    revised = Catalog(document)
    return revised, {'before_digest': catalog.fingerprint, 'after_digest': revised.fingerprint, 'row_counts': counts, 'mode': mode}


def difference(before, after):
    changes = []
    for table in SCHEMAS:
        old = {key_for(table, row): row for row in before.document[table]}
        new = {key_for(table, row): row for row in after.document[table]}
        for key in sorted(old.keys() | new.keys()):
            if key not in old:
                changes.append({'table': table, 'action': 'add', 'key': list(key), 'row': new[key]})
            elif key not in new:
                changes.append({'table': table, 'action': 'remove', 'key': list(key)})
            elif old[key] != new[key]:
                changes.append({'table': table, 'action': 'replace', 'key': list(key), 'row': new[key]})
    rebuilt, evidence = apply_patch(before, changes)
    for table in SCHEMAS:
        actual = sorted(rebuilt.document[table], key=lambda row: key_for(table, row))
        expected = sorted(after.document[table], key=lambda row: key_for(table, row))
        if actual != expected:
            raise ValueError('difference cannot reproduce target records')
    return {'operations': changes, 'evidence': evidence, 'array_order_not_preserved': True}
