from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import calendar
import hashlib
import json


class InvalidInput(ValueError):
    def __init__(self, path, message):
        self.path = path
        super().__init__(f'{path}: {message}')


def fields(row, required, optional=(), path='$'):
    if not isinstance(row, dict):
        raise InvalidInput(path, 'expected object')
    if set(required) - row.keys():
        raise InvalidInput(path, 'missing fields')
    if row.keys() - set(required) - set(optional):
        raise InvalidInput(path, 'unexpected fields')


def word(value, path):
    if not isinstance(value, str) or not value.strip() or len(value) > 120:
        raise InvalidInput(path, 'expected nonempty text')
    if any(ord(c) < 32 for c in value):
        raise InvalidInput(path, 'control characters forbidden')
    return value


def integer(value, path, low=0, high=10**12):
    if type(value) is not int or not low <= value <= high:
        raise InvalidInput(path, f'expected integer {low}..{high}')
    return value


def boolean(value, path):
    if type(value) is not bool:
        raise InvalidInput(path, 'expected boolean')
    return value


def iso(value, path):
    if not isinstance(value, str):
        raise InvalidInput(path, 'expected ISO date')
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidInput(path, 'invalid calendar date') from exc
    if parsed.isoformat() != value:
        raise InvalidInput(path, 'expected YYYY-MM-DD')
    return value


def sequence(value, path, maximum=100000):
    if not isinstance(value, list) or len(value) > maximum:
        raise InvalidInput(path, 'expected bounded list')
    return value


def unique(rows, path, key='id'):
    result = {}
    for row in rows:
        if row[key] in result:
            raise InvalidInput(path, 'duplicate identifier')
        result[row[key]] = row
    return result


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def cents(value):
    return int(Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def days(start, end, maximum=3660):
    a = date.fromisoformat(iso(start, 'start'))
    b = date.fromisoformat(iso(end, 'end'))
    count = (b - a).days
    if not 0 < count <= maximum:
        raise InvalidInput('range', 'expected positive bounded interval')
    return [(a + timedelta(days=i)).isoformat() for i in range(count)]


def month_start(value):
    parsed = date.fromisoformat(iso(value, 'day'))
    return parsed.replace(day=1).isoformat()


def month_end(value):
    parsed = date.fromisoformat(iso(value, 'day'))
    if parsed.month == 12:
        return parsed.replace(year=parsed.year + 1, month=1, day=1).isoformat()
    return parsed.replace(month=parsed.month + 1, day=1).isoformat()


def months(start, end):
    days(start, end)
    result = []
    current = month_start(start)
    while current < end:
        result.append(current[:7])
        current = month_end(current)
    return result


def age_months(birth, day):
    born = date.fromisoformat(iso(birth, 'birth'))
    current = date.fromisoformat(iso(day, 'day'))
    if current < born:
        return -1
    anniversary = min(born.day, calendar.monthrange(current.year, current.month)[1])
    return (current.year - born.year) * 12 + current.month - born.month - (current.day < anniversary)


def overlaps(a, b, c, d):
    return a < d and c < b
