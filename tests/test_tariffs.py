import copy
import json
from pathlib import Path
from decimal import Decimal
import unittest
from tariffs.catalog import Catalog
from tariffs.primitives import InvalidInput, days, months, age_months, month_start, month_end, canonical, digest
from tariffs.calendar import is_open, month_service_days, calendar_audit, capacity_calendar, first_once_day
from tariffs.charges import calculate, child_months
from tariffs.rounding import allocate_exact, capped_proportional, monthly_split
from tariffs.subsidies import eligibility, requests, apply
from analysis.engine import evaluate
from tariffs.coverage import fee_coverage, occupancy_summary, age_transitions, tariff_change_points


class TariffTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(Path('fixtures/county.json').read_text())
        self.catalog = Catalog(self.data)
        self.start = '2026-11-01'
        self.end = '2026-12-01'

    def minimal(self):
        data = copy.deepcopy(self.data)
        data['households'] = data['households'][:1]
        data['households'][0]['children'] = data['households'][0]['children'][:1]
        data['enrollments'] = data['enrollments'][:1]
        data['fees'] = data['fees'][:1]
        data['subsidies'] = []
        return data

    def test_calendar_half_open(self):
        self.assertEqual(len(days(self.start, self.end)), 30)
        self.assertEqual(days('2024-02-28', '2024-03-01'), ['2024-02-28', '2024-02-29'])
        self.assertEqual(months('2026-11-30', '2027-01-01'), ['2026-11', '2026-12'])
        self.assertEqual(month_start('2026-11-30'), self.start)
        self.assertEqual(month_end('2026-12-30'), '2027-01-01')
        with self.assertRaises(InvalidInput):
            days(self.end, self.start)
        with self.assertRaises(InvalidInput):
            days(self.start, self.start)
        with self.assertRaises(InvalidInput):
            days('2026-2-01', self.end)

    def test_age_month_end(self):
        self.assertEqual(age_months('2024-01-31', '2024-02-28'), 0)
        self.assertEqual(age_months('2024-01-31', '2024-02-29'), 1)
        self.assertEqual(age_months('2025-01-31', '2025-02-28'), 1)
        self.assertEqual(age_months('2025-01-31', '2025-03-30'), 1)
        self.assertEqual(age_months('2025-01-31', '2025-03-31'), 2)
        self.assertEqual(age_months('2025-01-31', '2025-01-30'), -1)

    def test_special_service_days(self):
        provider = self.catalog.providers['q']
        self.assertFalse(is_open(provider, '2026-11-02'))
        self.assertTrue(is_open(provider, '2026-11-07'))
        self.assertFalse(is_open(provider, '2026-11-08'))
        service = month_service_days(provider, self.start)
        self.assertEqual(len(service), 21)
        self.assertNotIn('2026-11-02', service)
        self.assertIn('2026-11-07', service)

    def test_full_month_and_deposits(self):
        result = calculate(self.catalog, self.start, self.end)
        first = [row for row in result['charges'] if row['child'] == 'h0']
        fees = {row['fee']: row for row in first}
        self.assertEqual(fees['care']['gross_cents'], 30000)
        self.assertEqual(fees['meals']['gross_cents'], 2400)
        self.assertEqual(fees['security']['gross_cents'], 5000)
        self.assertTrue(fees['security']['refundable'])
        self.assertEqual(len(fees['care']['daily_cents']), 30)
        self.assertEqual(sum(fees['care']['daily_cents'].values()), 30000)
        rows = child_months(self.catalog, result, self.start, self.end)
        self.assertEqual(rows[0]['operating_cents'], 32400)
        self.assertEqual(rows[0]['deposit_cents'], 5000)
        self.assertEqual(result['gross_cents'], 5 * 37400)

    def test_calendar_proration(self):
        data = self.minimal()
        data['enrollments'][0]['start'] = '2026-11-16'
        data['enrollments'][0]['attendance'] = []
        catalog = Catalog(data)
        charges = calculate(catalog, self.start, self.end)['charges']
        self.assertEqual(len(charges), 1)
        self.assertEqual(charges[0]['gross_cents'], 15000)
        self.assertEqual(len(charges[0]['daily_cents']), 15)
        later = calculate(catalog, '2026-11-21', self.end)['charges']
        self.assertEqual(later[0]['gross_cents'], 10000)
        self.assertEqual(len(later[0]['daily_cents']), 10)

    def test_service_proration(self):
        data = self.minimal()
        data['enrollments'][0]['provider'] = 'q'
        data['fees'][0]['provider'] = 'q'
        data['fees'][0]['proration'] = 'service'
        data['enrollments'][0]['attendance'] = []
        data['fees'][0]['amount'] = 21000
        result = calculate(Catalog(data), self.start, self.end)
        self.assertEqual(result['gross_cents'], 21000)
        days_charged = result['charges'][0]['daily_cents']
        self.assertEqual(len(days_charged), 21)
        self.assertNotIn('2026-11-02', days_charged)
        self.assertEqual(set(days_charged.values()), {1000})

    def test_midmonth_version(self):
        data = self.minimal()
        data['fees'][0]['end'] = '2026-11-16'
        second = {**data['fees'][0], 'start': '2026-11-16', 'end': '2027-01-01', 'amount': 60000}
        data['fees'].append(second)
        charges = calculate(Catalog(data), self.start, self.end)['charges']
        self.assertEqual(len(charges), 1)
        self.assertEqual(charges[0]['gross_cents'], 45000)
        self.assertEqual(charges[0]['daily_cents']['2026-11-15'], 1000)
        self.assertEqual(charges[0]['daily_cents']['2026-11-16'], 2000)
        second['kind'] = 'once'
        with self.assertRaisesRegex(InvalidInput, 'stable'):
            Catalog(data)

    def test_optional_and_daily(self):
        data = self.minimal()
        data['fees'][0]['kind'] = 'daily'
        data['fees'][0]['amount'] = 100
        data['fees'][0]['required'] = False
        catalog = Catalog(data)
        self.assertEqual(calculate(catalog, self.start, self.end)['charges'], [])
        data['enrollments'][0]['options'] = ['care']
        data['providers'][0]['closed'] = ['2026-11-01']
        result = calculate(Catalog(data), self.start, self.end)
        self.assertEqual(result['gross_cents'], 2900)
        self.assertEqual(len(result['charges'][0]['daily_cents']), 29)

    def test_once_not_recharged_in_later_window(self):
        data = self.minimal()
        data['fees'][0]['kind'] = 'once'
        data['enrollments'][0]['end'] = '2027-01-01'
        catalog = Catalog(data)
        first = calculate(catalog, self.start, self.end)
        second = calculate(catalog, self.end, '2027-01-01')
        self.assertEqual(first['gross_cents'], 30000)
        self.assertEqual(second['gross_cents'], 0)
        self.assertEqual(first['charges'][0]['daily_cents'], {self.start: 30000})
        self.assertEqual(first_once_day(data['fees'], data['enrollments'][0], catalog.households.children['h0']), self.start)

    def test_closed_attendance_is_error(self):
        data = self.minimal()
        data['providers'][0]['closed'] = ['2026-11-03']
        catalog = Catalog(data)
        checked = calendar_audit(catalog, self.start, self.end)
        self.assertEqual(checked[0]['attendance_on_closed_days'], ['2026-11-03'])
        with self.assertRaisesRegex(InvalidInput, 'closed day'):
            calculate(catalog, self.start, self.end)

    def test_largest_remainder(self):
        result = allocate_exact({'a': Decimal('1.4'), 'b': Decimal('1.4'), 'c': Decimal('1.4')})
        self.assertEqual(result, {'a': 2, 'b': 1, 'c': 1})
        self.assertEqual(sum(result.values()), 4)
        self.assertEqual(allocate_exact({}), {})
        with self.assertRaises(ValueError):
            allocate_exact({'x': Decimal('-1')})
        split = monthly_split(10, ['a', 'b', 'c'])
        self.assertEqual(split, {'a': 4, 'b': 3, 'c': 3})
        with self.assertRaises(ValueError):
            monthly_split(1, ['a', 'a'])

    def test_proportional_cap(self):
        actual = capped_proportional({'a': 5, 'b': 5, 'c': 1}, 7)
        self.assertEqual(sum(actual.values()), 7)
        self.assertEqual(actual, {'a': 3, 'b': 3, 'c': 1})
        self.assertEqual(capped_proportional({'a': 0}, 0), {'a': 0})
        self.assertEqual(capped_proportional({'a': 4}, 5), {'a': 4})
        with self.assertRaises(InvalidInput):
            capped_proportional({'a': True}, 4)
        for cap in range(21):
            allocated = capped_proportional({'a': 9, 'b': 11}, cap)
            self.assertEqual(sum(allocated.values()), cap)
            self.assertLessEqual(allocated['a'], 9)
            self.assertLessEqual(allocated['b'], 11)

    def test_shared_family_cap(self):
        result = evaluate(self.catalog, self.start, self.end)
        allocations = result['subsidies']['allocations']
        family = [row for row in allocations if row['household'] == 'h' and row['program'] == 'family']
        self.assertEqual(len(family), 2)
        self.assertEqual([row['requested_cents'] for row in family], [16200, 16200])
        self.assertEqual([row['granted_cents'] for row in family], [10000, 10000])
        h = next(row for row in result['families'] if row['household'] == 'h')
        self.assertEqual(h['operating_cents'], 64800)
        self.assertEqual(h['subsidy_cents'], 26000)
        self.assertEqual(h['net_operating_cents'], 38800)
        self.assertEqual(h['deposit_cents'], 10000)
        self.assertEqual(h['cash_outlay_cents'], 48800)

    def test_eligibility_boundaries(self):
        program = self.catalog.subsidies['family']
        household = self.catalog.households.by_id['h']
        child = self.catalog.households.children['h0']
        self.assertEqual(eligibility(program, household, child, self.start, 3000000), (True, 'eligible'))
        self.assertEqual(eligibility(program, household, child, self.start, 3000001), (False, 'income_above_ceiling'))
        self.assertEqual(eligibility(program, household, child, self.start, None), (False, 'income_unknown'))
        self.assertEqual(eligibility(program, household, child, '2027-01-01', 0)[1], 'outside_program_validity')
        restricted = {**program, 'region': 'west'}
        self.assertEqual(eligibility(restricted, household, child, self.start, 0)[1], 'region_not_eligible')
        self.assertEqual(eligibility({**program, 'max_age': 12}, household, child, self.start, 0)[1], 'age_not_eligible')

    def test_stack_choice_and_gross_cap(self):
        data = self.minimal()
        base = copy.deepcopy(self.data['subsidies'][0])
        base.update(mode='flat', value=90000, family_month_cap=90000)
        other = {**base, 'id': 'better', 'priority': 8}
        third = {**base, 'id': 'third', 'stack_group': 'other', 'priority': 1}
        data['subsidies'] = [base, other, third]
        result = evaluate(Catalog(data), self.start, self.end)
        self.assertEqual(result['totals']['subsidy_cents'], 30000)
        self.assertEqual(result['totals']['net_operating_cents'], 0)
        allocations = {row['program']: row for row in result['subsidies']['allocations']}
        self.assertNotIn('family', allocations)
        self.assertEqual(allocations['better']['granted_cents'], 30000)
        self.assertEqual(allocations['third']['granted_cents'], 0)
        self.assertEqual(allocations['third']['gross_cap_reduction_cents'], 30000)

    def test_capacity_and_coverage(self):
        calendar = capacity_calendar(self.catalog, self.start, self.end)
        first = next(row for row in calendar if row['provider'] == 'p' and row['day'] == self.start)
        self.assertEqual(first['enrolled'], 5)
        self.assertEqual(first['enrollment_excess'], 2)
        summary = occupancy_summary(self.catalog, self.start, self.end)
        p = next(row for row in summary if row['provider'] == 'p')
        self.assertEqual(p['place_days'], 90)
        self.assertEqual(p['enrolled_place_days'], 150)
        self.assertEqual(p['enrollment_excess_place_days'], 60)
        coverage = fee_coverage(self.catalog, self.start, self.end)
        self.assertTrue(all(not row['open_days_without_required_operating_fee'] for row in coverage))
        points = tariff_change_points(self.catalog, '2026-01-01', '2027-01-02')
        self.assertEqual(points[0]['day'], '2026-01-01')
        self.assertEqual(points[-1]['day'], '2027-01-01')
        transitions = age_transitions(self.catalog, self.start, self.end)
        self.assertEqual(transitions[-1]['day'], '2026-11-30')

    def test_catalog_rejects_structural_errors(self):
        modifications = [
            lambda d: d.update(version=True),
            lambda d: d['providers'][0].update(capacity=True),
            lambda d: d['providers'][0].update(weekdays=[0, 0]),
            lambda d: d['providers'][0].update(closed=[self.start], extra_open=[self.start]),
            lambda d: d['households'][0].update(income_low=20, income_high=10),
            lambda d: d['households'][0].update(income_high=None),
            lambda d: d['households'][0].update(weight=0),
            lambda d: d['enrollments'][0].update(child='unknown'),
            lambda d: d['enrollments'][0].update(provider='unknown'),
            lambda d: d['enrollments'][0].update(options=['unknown']),
            lambda d: d['enrollments'][0].update(end='2040-01-01'),
            lambda d: d['fees'][0].update(kind='weekly'),
            lambda d: d['fees'][0].update(max_age=0),
            lambda d: d['fees'][0].update(amount=-1),
            lambda d: d['subsidies'][0].update(value=10001),
        ]
        for modify in modifications:
            with self.subTest(modify=modify):
                data = copy.deepcopy(self.data)
                modify(data)
                with self.assertRaises(ValueError):
                    Catalog(data)

    def test_catalog_snapshot_isolation(self):
        fingerprint = self.catalog.fingerprint
        self.data['providers'][0]['capacity'] = 900
        self.assertEqual(self.catalog.providers['p']['capacity'], 3)
        exported = self.catalog.export()
        exported['providers'][0]['capacity'] = 100
        self.assertEqual(self.catalog.fingerprint, fingerprint)
        self.assertNotEqual(digest(exported), fingerprint)
        self.assertEqual(digest(self.catalog.document), fingerprint)


if __name__ == '__main__':
    unittest.main()
