import csv
import io
import json
from pathlib import Path
from tariffs.primitives import canonical
from tariffs.catalog import Catalog


SCHEMAS = {
    'providers': {'id': 'str', 'region': 'str', 'capacity': 'int', 'weekdays': 'json', 'closed': 'json', 'extra_open': 'json'},
    'households': {'id': 'str', 'region': 'str', 'income_low': 'nullable_int', 'income_high': 'nullable_int', 'weight': 'int', 'children': 'json'},
    'fees': {'id': 'str', 'provider': 'str', 'start': 'str', 'end': 'str', 'kind': 'str', 'amount': 'int', 'required': 'bool', 'proration': 'str', 'min_age': 'int', 'max_age': 'int', 'refundable': 'bool'},
    'subsidies': {'id': 'str', 'start': 'str', 'end': 'str', 'region': 'str', 'mode': 'str', 'value': 'int', 'income_ceiling': 'int', 'min_age': 'int', 'max_age': 'int', 'stack_group': 'str', 'priority': 'int', 'family_month_cap': 'int'},
    'enrollments': {'id': 'str', 'child': 'str', 'provider': 'str', 'start': 'str', 'end': 'str', 'options': 'json', 'attendance': 'json'},
}


def convert(value, kind):
    if kind == 'str':
        return value
    if kind == 'bool':
        if value not in ('true', 'false'):
            raise ValueError('CSV boolean must be true or false')
        return value == 'true'
    if kind == 'nullable_int' and value == '':
        return None
    if kind in ('int', 'nullable_int'):
        if not value or not value.isascii() or not value.isdigit():
            raise ValueError('CSV numeric cell must be unsigned decimal integer')
        return int(value)
    if kind == 'json':
        return json.loads(value)
    raise ValueError('unknown column type')


def read_table(content, name):
    if name not in SCHEMAS:
        raise ValueError('unknown table')
    if not isinstance(content, str) or len(content.encode()) > 50_000_000:
        raise ValueError('CSV table must be bounded text')
    schema = SCHEMAS[name]
    try:
        reader = csv.reader(io.StringIO(content), strict=True)
        header = next(reader, None)
        if not header or set(header) != set(schema) or len(header) != len(set(header)):
            raise ValueError('CSV header does not match schema')
        rows = []
        for values in reader:
            if len(values) != len(header):
                raise ValueError('CSV row width mismatch')
            rows.append({key: convert(value, schema[key]) for key, value in zip(header, values)})
            if len(rows) > 100000:
                raise ValueError('too many CSV rows')
        return rows
    except csv.Error as exc:
        raise ValueError('malformed CSV') from exc


def write_table(document, name):
    Catalog(document)
    if name not in SCHEMAS:
        raise ValueError('unknown table')
    schema = SCHEMAS[name]
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator='\n')
    writer.writerow(schema)
    for row in document[name]:
        values = []
        for key, kind in schema.items():
            value = row[key]
            if kind == 'json':
                values.append(canonical(value))
            elif kind == 'bool':
                values.append('true' if value else 'false')
            else:
                values.append('' if value is None else value)
        writer.writerow(values)
    return stream.getvalue()


def read_directory(directory):
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('CSV directory must be regular directory')
    if {path.name for path in root.iterdir()} != {name + '.csv' for name in SCHEMAS}:
        raise ValueError('CSV directory has wrong file set')
    document = {'version': 1}
    for name in SCHEMAS:
        path = root / (name + '.csv')
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 50_000_000:
            raise ValueError('CSV table must be bounded regular file')
        document[name] = read_table(path.read_text(encoding='utf-8-sig'), name)
    return Catalog(document)


def write_directory(document, directory):
    catalog = Catalog(document)
    root = Path(directory)
    if root.exists() or root.is_symlink():
        raise FileExistsError(root)
    root.mkdir(parents=True)
    written = []
    try:
        for name in SCHEMAS:
            path = root / (name + '.csv')
            path.write_text(write_table(catalog.document, name), encoding='utf-8')
            written.append(path)
    except BaseException:
        for path in written:
            path.unlink()
        root.rmdir()
        raise
    return {'files': sorted(path.name for path in written), 'version': 1}
