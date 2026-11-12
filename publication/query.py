import base64
import json
from tariffs.primitives import canonical, digest, integer
from analysis.engine import verify


FIELDS = {'household', 'month', 'region', 'income_band', 'weight', 'children_enrolled', 'operating_cents', 'deposit_cents', 'subsidy_cents', 'net_operating_cents', 'cash_outlay_cents', 'burden', 'income_missing', 'income_assumed', 'positive_cost_zero_income'}
FILTERS = {'household', 'month', 'region', 'income_band'}


def encode(value):
    return base64.urlsafe_b64encode(canonical(value).encode()).decode().rstrip('=')


def decode(value):
    if not isinstance(value, str) or len(value) > 10000:
        raise ValueError('invalid cursor')
    try:
        parsed = json.loads(base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True))
    except (ValueError, UnicodeError) as exc:
        raise ValueError('invalid cursor') from exc
    if not isinstance(parsed, dict):
        raise ValueError('cursor must contain object')
    return parsed


def families(result, filters=None, columns=None, limit=100, cursor=None):
    verify(result)
    filters = filters or {}
    columns = sorted(FIELDS) if columns is None else columns
    if not isinstance(filters, dict) or filters.keys() - FILTERS:
        raise ValueError('unsupported query filter')
    for values in filters.values():
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values) or len(values) != len(set(values)):
            raise ValueError('filters require unique string lists')
    if not isinstance(columns, list) or not columns or len(columns) != len(set(columns)) or set(columns) - FIELDS:
        raise ValueError('invalid projection')
    integer(limit, 'limit', 1, 1000)
    context = digest({'result': result['result_digest'], 'filters': filters, 'columns': columns})
    rows = [row for row in result['families'] if all(row[key] in values for key, values in filters.items())]
    rows.sort(key=lambda row: (row['household'], row['month']))
    offset = 0
    if cursor is not None:
        token = decode(cursor)
        if set(token) != {'context', 'offset'} or token['context'] != context:
            raise ValueError('cursor belongs to another query or analysis')
        offset = integer(token['offset'], 'offset', 0, len(rows))
    selected = rows[offset:offset + limit]
    next_offset = offset + len(selected)
    continuation = encode({'context': context, 'offset': next_offset}) if next_offset < len(rows) else None
    return {'result_digest': result['result_digest'], 'total': len(rows), 'offset': offset, 'rows': [{key: row[key] for key in columns} for row in selected], 'next_cursor': continuation, 'access': 'restricted_family_analysis'}
