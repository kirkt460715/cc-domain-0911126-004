import copy
import json
from pathlib import Path
from decimal import Decimal
import random
import unittest
from analysis.engine import evaluate, verify
from analysis import weighted
from analysis.groups import grouped, income_missingness
from analysis.quality import audit_result, input_quality
from analysis.providers import provider_attribution, provider_report, subsidy_incidence
from analysis.variants import change, sensitivity, paired_families
from analysis.comparison import compare, endpoint_envelope
from analysis.income_grid import critical_incomes, household_envelope
from analysis.explanations import family_statement, fee_contributions, subsidy_waterfall, eligibility_reasons
from analysis.distribution import lorenz, concentration, burden_bands, region_gaps
from analysis.policy_review import program_overlap, leave_one_program_out, budget_sweep, remove_programs, program_recipients
from analysis.service_use import cross_region_flows, attendance_costs, enrollment_movements, cost_by_age
from analysis.reconciliation import reconcile, provider_differences, CLAIM_METRICS
from households.bands import income_band, income_interval_bands, validate_bands
from tariffs.catalog import Catalog
from tariffs.primitives import digest


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(Path('fixtures/county.json').read_text())
        self.catalog = Catalog(self.data)
        self.start = '2026-11-01'
        self.end = '2026-12-01'
        self.result = evaluate(self.catalog, self.start, self.end)

    def family(self, ident, result=None):
        return next(row for row in (result or self.result)['families'] if row['household'] == ident)

    def test_income_states(self):
        h = self.family('h')
        missing = self.family('missing')
        zero = self.family('zero')
        nonuser = self.family('nonuser')
        self.assertEqual(h['period_income_cents'], 100000)
        self.assertEqual(h['burden'], '0.388000000000')
        self.assertFalse(h['income_assumed'])
        self.assertTrue(missing['income_missing'])
        self.assertIsNone(missing['burden'])
        self.assertEqual(missing['subsidy_cents'], 0)
        self.assertTrue(zero['positive_cost_zero_income'])
        self.assertIsNone(zero['burden'])
        self.assertFalse(zero['income_missing'])
        self.assertEqual(nonuser['net_operating_cents'], 0)
        self.assertEqual(nonuser['children_enrolled'], 0)

    def test_policy_validation_empty(self):
        data = self.catalog.export()
        data['households'] = []
        data['enrollments'] = []
        catalog = Catalog(data)
        self.assertEqual(evaluate(catalog, self.start, self.end)['families'], [])
        with self.assertRaises(ValueError):
            evaluate(catalog, self.start, self.end, endpoint='middle')
        with self.assertRaises(ValueError):
            evaluate(catalog, self.start, self.end, missing='zero')
        with self.assertRaises(ValueError):
            evaluate(catalog, self.start, self.end, missing='assume')
        with self.assertRaises(ValueError):
            evaluate(catalog, self.start, self.end, assumed_income=0)
        with self.assertRaises(ValueError):
            self.catalog.households.income('h', 'low', 'wrong')

    def test_assumption_and_partial_income(self):
        result = evaluate(self.catalog, self.start, self.end, missing='assume', assumed_income=1200000)
        row = self.family('missing', result)
        self.assertFalse(row['income_missing'])
        self.assertTrue(row['income_assumed'])
        self.assertEqual(row['period_income_cents'], 100000)
        self.assertEqual(row['subsidy_cents'], 19200)
        self.assertEqual(row['net_operating_cents'], 13200)
        self.assertTrue(audit_result(self.catalog, result, replay=True)['valid'])
        partial = self.family('h', evaluate(self.catalog, '2026-11-16', self.end))
        self.assertEqual(partial['period_income_cents'], 50000)
        self.assertEqual(partial['exposure_days'], 15)
        self.assertEqual(partial['deposit_cents'], 0)
        self.assertEqual(partial['operating_cents'], 30000)
        self.assertEqual(partial['subsidy_cents'], 18000)
        self.assertEqual(partial['burden'], '0.240000000000')

    def test_statistics_independent_expansion(self):
        rng = random.Random(41)
        for size in range(1, 20):
            values = [(rng.randrange(100), rng.randrange(1, 5)) for _ in range(size)]
            expanded = sorted(value for value, weight in values for _ in range(weight))
            self.assertEqual(weighted.mean(values), Decimal(sum(expanded)) / len(expanded))
            for p in ('0', '.1', '.5', '.9', '1'):
                threshold = Decimal(p) * len(expanded)
                index = max(0, int(threshold.to_integral_value(rounding='ROUND_CEILING')) - 1)
                self.assertEqual(weighted.quantile(values, p), expanded[index])
            delta = sum(abs(a - b) for a in expanded for b in expanded)
            expected = Decimal(delta) / (2 * len(expanded) * sum(expanded)) if sum(expanded) else None
            self.assertEqual(weighted.gini(values), expected)
            self.assertEqual(weighted.threshold_share(values, 50), Decimal(sum(v > 50 for v in expanded)) / len(expanded))

    def test_statistics_edges(self):
        self.assertIsNone(weighted.mean([]))
        self.assertIsNone(weighted.quantile([], '.5'))
        self.assertIsNone(weighted.gini([(0, 1)]))
        self.assertEqual(weighted.trimmed_mean([(0, 1), (10, 8), (100, 1)], '.1'), 10)
        self.assertEqual(weighted.effective_sample_size([2, 2]), 2)
        self.assertEqual(weighted.threshold_share([(1, 1)], 1), 0)
        self.assertEqual(weighted.threshold_share([(1, 1)], 1, strict=False), 1)
        with self.assertRaises(ValueError):
            weighted.mean([('NaN', 1)])
        with self.assertRaises(ValueError):
            weighted.quantile([(1, 1)], 2)
        with self.assertRaises(ValueError):
            weighted.trimmed_mean([(1, 1)], '.5')
        with self.assertRaises(ValueError):
            weighted.gini([(-1, 1)])

    def test_group_denominators(self):
        report = grouped(self.result, [])['rows'][0]
        self.assertEqual(report['distinct_households'], 4)
        self.assertEqual(report['weight'], 7)
        self.assertEqual(report['income_missing_weight'], 3)
        self.assertEqual(report['positive_cost_zero_income_weight'], 1)
        self.assertEqual(report['burden_summary']['observations'], 2)
        self.assertEqual(report['burden_summary']['weight'], 3)
        all_report = grouped(self.result, [], 'all')['rows'][0]
        self.assertEqual(all_report['distinct_households'], 5)
        self.assertEqual(all_report['weight'], 8)
        self.assertEqual(all_report['burden_summary']['observations'], 3)
        self.assertNotEqual(report['ratio_of_totals'], report['burden_summary']['weighted_mean'])
        with self.assertRaises(ValueError):
            grouped(self.result, ['household'])
        with self.assertRaises(ValueError):
            grouped(self.result, ['region', 'region'])
        with self.assertRaises(ValueError):
            grouped(self.result, [], burden_threshold='NaN')

    def test_audit_missingness(self):
        east = next(row for row in income_missingness(self.result) if row['region'] == 'east')
        self.assertEqual(east['missing_weight'], 3)
        self.assertEqual(east['weight'], 6)
        self.assertEqual(east['missing_share'], '0.500000000000')
        checked = audit_result(self.catalog, self.result, replay=True)
        self.assertTrue(checked['valid'])
        self.assertTrue(checked['full_replay_performed'])
        self.assertEqual(checked['provider_over_capacity_days'], 30)
        quality = input_quality(self.catalog, self.start, self.end)
        self.assertEqual(quality['counts']['warning'], 1)
        self.assertIn('income_interval', {row['code'] for row in quality['findings']})
        modified = copy.deepcopy(self.result)
        modified['families'][0]['net_operating_cents'] += 1
        with self.assertRaises(ValueError):
            verify(modified)

    def test_provider_conservation(self):
        rows = provider_attribution(self.catalog, self.result)
        self.assertEqual(sum(row['net_cents'] for row in rows), self.result['totals']['net_operating_cents'])
        self.assertEqual(sum(row['attributed_subsidy_cents'] for row in rows), self.result['totals']['subsidy_cents'])
        report = provider_report(self.catalog, self.result)
        self.assertEqual(report['rows'][0]['distinct_children'], 5)
        self.assertEqual(report['rows'][0]['distinct_households'], 4)
        self.assertEqual(report['rows'][0]['cross_region_children'], 2)
        incidence = subsidy_incidence(self.catalog, self.result)
        self.assertEqual(sum(Decimal(row['subsidy_share']) for row in incidence['rows']), 1)
        self.assertEqual(incidence['interpretation'], 'weighted_sample_incidence_not_population_fiscal_total')

    def test_scenario_pairing(self):
        catalog = change(self.data, [{'table': 'subsidies', 'id': 'family', 'field': 'family_month_cap', 'value': 0}])
        result = evaluate(catalog, self.start, self.end)
        paired = compare(self.result, result)
        h = next(row for row in paired['paired'] if row['household'] == 'h')
        self.assertEqual(h['net_change_cents'], 20000)
        self.assertEqual(paired['unpaired'], [])
        self.assertEqual(self.catalog.subsidies['family']['family_month_cap'], 20000)
        changed = change(self.data, [{'table': 'households', 'id': 'h', 'field': 'weight', 'value': 9}])
        compared = compare(self.result, evaluate(changed, self.start, self.end))
        self.assertEqual(compared['excluded_weight_changed_cells'], [{'household': 'h', 'month': '2026-11'}])
        with self.assertRaises(ValueError):
            change(self.data, [{'table': 'fees', 'id': 'care', 'field': 'amount', 'value': 0}])
        with self.assertRaises(ValueError):
            paired_families(self.result, evaluate(self.catalog, '2026-11-02', self.end))

    def test_income_threshold_grid(self):
        self.assertEqual(critical_incomes(self.catalog, 'i'), [2400000, 3000000, 3000001, 4800000])
        envelope = household_envelope(self.catalog, 'i', self.start, self.end)
        self.assertEqual(len(envelope['cases']), 4)
        high = evaluate(self.catalog, self.start, self.end, endpoint='high')
        endpoints = endpoint_envelope(self.result, high)
        i = next(row for row in endpoints['rows'] if row['household'] == 'i')
        self.assertTrue(i['subsidy_eligibility_changes'])
        self.assertGreater(Decimal(envelope['envelopes'][0]['maximum_burden']), Decimal(i['maximum_endpoint_burden']))
        with self.assertRaises(ValueError):
            household_envelope(self.catalog, 'missing', self.start, self.end)
        with self.assertRaises(ValueError):
            endpoint_envelope(high, self.result)

    def test_explanations(self):
        statement = family_statement(self.catalog, self.result, 'h')
        self.assertEqual(len(statement['months']), 1)
        self.assertEqual(len(statement['months'][0]['children']), 2)
        self.assertEqual(sum(row['net_operating_cents'] for row in statement['months'][0]['children']), 38800)
        self.assertEqual(statement['statement_digest'], digest({k: v for k, v in statement.items() if k != 'statement_digest'}))
        waterfall = subsidy_waterfall(self.result)
        family = next(row for row in waterfall if row['program'] == 'family')
        self.assertEqual(family['family_cap_reduction_cents'], 12400)
        self.assertEqual(family['child_cap_reduction_cents'], 0)
        reasons = next(row for row in eligibility_reasons(self.result) if row['program'] == 'family')
        self.assertEqual(reasons['child_days_by_reason']['income_unknown'], 30)
        self.assertEqual(sum(row['gross_cents'] for row in fee_contributions(self.result)), 187000)
        with self.assertRaises(KeyError):
            family_statement(self.catalog, self.result, 'absent')

    def test_distribution_reports(self):
        curve = lorenz(self.result)
        self.assertEqual(curve['points'][0]['population_share'], '0.000000000000')
        self.assertEqual(curve['points'][-1]['amount_share'], '1.000000000000')
        self.assertEqual(curve['points'][-1]['population_share'], '1.000000000000')
        report = concentration(self.result)
        self.assertEqual(report['points'][-1]['benefit_share'], '1.000000000000')
        self.assertEqual(report['known_income_weight'], 4)
        bands = burden_bands(self.result)
        self.assertIn('unknown_income', {row['band'] for row in bands})
        self.assertIn('zero_income_positive_cost', {row['band'] for row in bands})
        self.assertEqual(sum(row['weight'] for row in bands), 7)
        self.assertEqual(len(region_gaps(self.result)), 2)
        self.assertEqual(cost_by_age(self.result)[0]['child_months'], 5)
        with self.assertRaises(ValueError):
            burden_bands(self.result, ['0', '.2', '.1'])
        with self.assertRaises(ValueError):
            lorenz(self.result, metric='income')

    def test_program_sweeps(self):
        overlaps = program_overlap(self.catalog, self.start, self.end)
        self.assertEqual(len(overlaps), 1)
        self.assertFalse(overlaps[0]['same_stack_group'])
        omitted = leave_one_program_out(self.catalog, self.result)
        local = next(row for row in omitted['rows'] if row['removed_program'] == 'local')
        self.assertEqual(local['net_cost_increase_cents'], 6000)
        sweep = budget_sweep(self.catalog, self.result, 'family', [0, 10000, 20000, 40000])
        grants = [row['total_subsidy_cents'] for row in sweep['rows']]
        self.assertEqual(grants, sorted(grants))
        self.assertEqual(sweep['rows'][2]['net_change_cents'], 0)
        family = next(row for row in program_recipients(self.result) if row['program'] == 'family')
        self.assertEqual(family['households'], ['h', 'i', 'zero'])
        with self.assertRaises(ValueError):
            remove_programs(self.catalog, ['missing'])
        with self.assertRaises(ValueError):
            budget_sweep(self.catalog, self.result, 'family', [1, 1])

    def test_sensitivity_bands(self):
        cases = [{'name': 'low', 'endpoint': 'low', 'missing': 'exclude'}, {'name': 'high', 'endpoint': 'high', 'missing': 'exclude'}]
        report = sensitivity(self.catalog, self.start, self.end, cases)
        self.assertNotEqual(report['cases'][0]['result_digest'], report['cases'][1]['result_digest'])
        self.assertEqual(report['cases'][0]['input_digest'], report['cases'][1]['input_digest'])
        with self.assertRaises(ValueError):
            sensitivity(self.catalog, self.start, self.end, [cases[0], cases[0]])
        self.assertEqual(income_band(2999999), 'low')
        self.assertEqual(income_band(3000000), 'middle')
        self.assertEqual(income_band(10000000), 'high')
        self.assertTrue(income_interval_bands(self.catalog.households.by_id['i'])['crosses_bands'])
        with self.assertRaises(ValueError):
            validate_bands([{'id': 'x', 'minimum': 1, 'maximum': None}])

    def test_service_usage(self):
        cross = next(row for row in cross_region_flows(self.catalog, self.result)['rows'] if row['cross_region'])
        self.assertEqual(cross['children'], 2)
        h = next(row for row in attendance_costs(self.catalog, self.result) if row['child'] == 'h0')
        self.assertEqual(h['service_days'], 30)
        self.assertEqual(h['attendance_days'], 2)
        self.assertEqual(h['net_per_attended_day_cents'], '9700.000000000000')
        self.assertEqual(enrollment_movements(self.catalog, self.start, self.end), [])
        data = self.catalog.export()
        data['enrollments'][0]['end'] = '2026-11-15'
        moved = {**data['enrollments'][0], 'id': 'next', 'start': '2026-11-15', 'end': self.end, 'provider': 'q', 'attendance': []}
        data['enrollments'].append(moved)
        movements = enrollment_movements(Catalog(data), self.start, self.end)
        self.assertTrue(movements[0]['continuous_contract'])
        self.assertTrue(movements[0]['provider_changed'])

    def test_reconciliation(self):
        expected = provider_attribution(self.catalog, self.result)
        claims = [{key: row[key] for key in ('provider', 'child', 'month', *CLAIM_METRICS)} for row in expected]
        report = reconcile(self.catalog, self.result, claims)
        self.assertTrue(report['matched'])
        self.assertEqual(report['counts']['matched'], 5)
        claims[0]['gross_cents'] += 1
        claims[0]['net_cents'] += 1
        report = reconcile(self.catalog, self.result, claims)
        self.assertFalse(report['matched'])
        self.assertEqual(report['counts']['amount_mismatch'], 1)
        self.assertTrue(reconcile(self.catalog, self.result, claims, 1)['matched'])
        self.assertEqual(provider_differences(report)[0]['exception_child_months'], 1)
        claims.pop()
        self.assertEqual(reconcile(self.catalog, self.result, claims)['counts']['missing_claim'], 1)
        with self.assertRaises(ValueError):
            reconcile(self.catalog, self.result, claims + [claims[0]])


if __name__ == '__main__':
    unittest.main()
