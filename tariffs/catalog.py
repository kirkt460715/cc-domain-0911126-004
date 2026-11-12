import copy
from tariffs.primitives import fields, word, integer, boolean, iso, sequence, unique, InvalidInput, digest, overlaps
from households.registry import Households, validate_enrollments


class Catalog:
    def __init__(self, document):
        self.document = copy.deepcopy(document)
        fields(self.document, ('version', 'providers', 'households', 'fees', 'subsidies', 'enrollments'))
        if type(self.document['version']) is not int or self.document['version'] != 1:
            raise InvalidInput('version', 'only version 1 supported')
        for row in sequence(self.document['providers'], 'providers'):
            fields(row, ('id', 'region', 'capacity', 'weekdays', 'closed', 'extra_open'))
            word(row['id'], 'provider.id')
            word(row['region'], 'provider.region')
            integer(row['capacity'], 'provider.capacity')
            for day in sequence(row['weekdays'], 'weekdays', 7):
                integer(day, 'weekday', 0, 6)
            if len(row['weekdays']) != len(set(row['weekdays'])):
                raise InvalidInput('weekdays', 'duplicate weekday')
            for key in ('closed', 'extra_open'):
                values = sequence(row[key], key, 3660)
                for day in values:
                    iso(day, key)
                if len(values) != len(set(values)):
                    raise InvalidInput(key, 'duplicate day')
            if set(row['closed']) & set(row['extra_open']):
                raise InvalidInput('calendar', 'day cannot be both closed and extra open')
        self.providers = unique(self.document['providers'], 'providers')
        self.households = Households(self.document['households'])
        self.enrollments = validate_enrollments(self.document['enrollments'], self.households, self.providers)
        self.fees = self._fees(self.document['fees'])
        self.subsidies = self._subsidies(self.document['subsidies'])
        self.fingerprint = digest(self.document)

    def _fees(self, rows):
        groups = {}
        for row in sequence(rows, 'fees'):
            fields(row, ('id', 'provider', 'start', 'end', 'kind', 'amount', 'required', 'proration', 'min_age', 'max_age', 'refundable'))
            word(row['id'], 'fee.id')
            if row['provider'] not in self.providers:
                raise InvalidInput('fees', 'unknown provider')
            iso(row['start'], 'fee.start')
            iso(row['end'], 'fee.end')
            if row['start'] >= row['end']:
                raise InvalidInput('fees', 'empty fee validity')
            if row['kind'] not in ('monthly', 'daily', 'attendance', 'once'):
                raise InvalidInput('fees', 'unknown fee kind')
            if row['proration'] not in ('calendar', 'service'):
                raise InvalidInput('fees', 'unknown proration basis')
            integer(row['amount'], 'fee.amount')
            boolean(row['required'], 'fee.required')
            boolean(row['refundable'], 'fee.refundable')
            integer(row['min_age'], 'fee.min_age', 0, 240)
            integer(row['max_age'], 'fee.max_age', 1, 241)
            if row['min_age'] >= row['max_age']:
                raise InvalidInput('fees', 'empty age band')
            groups.setdefault((row['provider'], row['id']), []).append(row)
        for key, versions in groups.items():
            if len({(v['kind'], v['refundable']) for v in versions}) != 1:
                raise InvalidInput('fees', 'logical fee kind and refundable classification must remain stable')
            ordered = sorted(versions, key=lambda row: row['start'])
            for left, right in zip(ordered, ordered[1:]):
                if overlaps(left['start'], left['end'], right['start'], right['end']):
                    raise InvalidInput('fees', 'fee validity versions overlap')
        for enrollment in self.enrollments.values():
            known = {key[1] for key in groups if key[0] == enrollment['provider']}
            if set(enrollment['options']) - known:
                raise InvalidInput('enrollments.options', 'unknown optional fee')
        return groups

    def _subsidies(self, rows):
        result = sequence(rows, 'subsidies')
        for row in result:
            fields(row, ('id', 'start', 'end', 'region', 'mode', 'value', 'income_ceiling', 'min_age', 'max_age', 'stack_group', 'priority', 'family_month_cap'))
            word(row['id'], 'subsidy.id')
            iso(row['start'], 'subsidy.start')
            iso(row['end'], 'subsidy.end')
            if row['start'] >= row['end']:
                raise InvalidInput('subsidies', 'empty program validity')
            word(row['region'], 'subsidy.region')
            if row['mode'] not in ('flat', 'percent'):
                raise InvalidInput('subsidies', 'unknown subsidy mode')
            integer(row['value'], 'subsidy.value', 0, 10000 if row['mode'] == 'percent' else 10**12)
            integer(row['income_ceiling'], 'subsidy.income_ceiling')
            integer(row['min_age'], 'subsidy.min_age', 0, 240)
            integer(row['max_age'], 'subsidy.max_age', 1, 241)
            if row['min_age'] >= row['max_age']:
                raise InvalidInput('subsidies', 'empty program age band')
            word(row['stack_group'], 'subsidy.stack_group')
            integer(row['priority'], 'subsidy.priority', 0, 1000)
            integer(row['family_month_cap'], 'subsidy.family_month_cap')
        return unique(result, 'subsidies')

    def export(self):
        return copy.deepcopy(self.document)
