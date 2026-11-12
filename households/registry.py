import copy
from tariffs.primitives import fields, word, integer, boolean, iso, sequence, unique, InvalidInput, overlaps, days


class Households:
    def __init__(self, rows):
        self.rows = copy.deepcopy(sequence(rows, 'households'))
        self.children = {}
        for index, row in enumerate(self.rows):
            path = f'households[{index}]'
            fields(row, ('id', 'region', 'income_low', 'income_high', 'weight', 'children'), path=path)
            word(row['id'], path + '.id')
            word(row['region'], path + '.region')
            integer(row['weight'], path + '.weight', 1, 1000000)
            low, high = row['income_low'], row['income_high']
            if low is None or high is None:
                if low is not None or high is not None:
                    raise InvalidInput(path, 'income bounds must both be present or both null')
            else:
                integer(low, path + '.income_low')
                integer(high, path + '.income_high')
                if low > high:
                    raise InvalidInput(path, 'reversed income interval')
            for child in sequence(row['children'], path + '.children', 30):
                fields(child, ('id', 'birth'))
                word(child['id'], path + '.child.id')
                iso(child['birth'], path + '.child.birth')
                if child['id'] in self.children:
                    raise InvalidInput(path, 'child belongs to multiple households')
                self.children[child['id']] = {**child, 'household': row['id']}
        self.by_id = unique(self.rows, 'households')

    def income(self, ident, endpoint, missing='exclude', assumed_income=None):
        if endpoint not in ('low', 'high'):
            raise ValueError('income endpoint must be low or high')
        if missing not in ('exclude', 'assume'):
            raise ValueError('invalid missing income policy')
        if missing == 'assume':
            integer(assumed_income, 'assumed_income')
        elif assumed_income is not None:
            raise ValueError('unexpected assumed income')
        row = self.by_id[ident]
        value = row['income_' + endpoint]
        if value is not None:
            return {'annual_cents': value, 'assumed': False, 'source': endpoint + '_bound'}
        if missing == 'exclude':
            return {'annual_cents': None, 'assumed': False, 'source': 'missing_excluded'}
        if missing != 'assume':
            raise ValueError('missing income mode must be exclude or assume')
        integer(assumed_income, 'assumed_income')
        return {'annual_cents': assumed_income, 'assumed': True, 'source': 'explicit_assumption'}

    def export(self):
        return copy.deepcopy(self.rows)


def validate_enrollments(rows, households, providers):
    result = copy.deepcopy(sequence(rows, 'enrollments'))
    per_child = {}
    for i, row in enumerate(result):
        path = f'enrollments[{i}]'
        fields(row, ('id', 'child', 'provider', 'start', 'end', 'options', 'attendance'), path=path)
        word(row['id'], path + '.id')
        if row['child'] not in households.children:
            raise InvalidInput(path, 'unknown child')
        if row['provider'] not in providers:
            raise InvalidInput(path, 'unknown provider')
        iso(row['start'], path + '.start')
        iso(row['end'], path + '.end')
        if row['start'] >= row['end']:
            raise InvalidInput(path, 'empty enrollment')
        days(row['start'], row['end'])
        birth = households.children[row['child']]['birth']
        if row['start'] < birth:
            raise InvalidInput(path, 'enrollment precedes birth')
        options = sequence(row['options'], path + '.options', 100)
        for option in options:
            word(option, path + '.options')
        if len(options) != len(set(options)):
            raise InvalidInput(path, 'duplicate optional fee selection')
        attendance = sequence(row['attendance'], path + '.attendance', 3660)
        for day in attendance:
            iso(day, path + '.attendance')
            if not row['start'] <= day < row['end']:
                raise InvalidInput(path, 'attendance outside enrollment')
        if len(attendance) != len(set(attendance)):
            raise InvalidInput(path, 'duplicate attendance day')
        per_child.setdefault(row['child'], []).append(row)
    mapping = unique(result, 'enrollments')
    for child, enrollments in per_child.items():
        ordered = sorted(enrollments, key=lambda row: (row['start'], row['end']))
        for left, right in zip(ordered, ordered[1:]):
            if overlaps(left['start'], left['end'], right['start'], right['end']):
                raise InvalidInput('enrollments', f'child {child} has overlapping providers or contracts')
    return mapping
