import copy
import json
from pathlib import Path
from decimal import Decimal
from fractions import Fraction
import tempfile
import unittest

from tariffs.catalog import Catalog
from analysis.contributions import (
    MAX_PERMUTATIONS,
    normalize_definition,
    run_study,
    resume_study,
    verify_study,
    replay_study,
    permutation_stream,
)
from publication.store import Archive, Conflict
from publication.service import Service
from publication import recovery
from nursery_report import demonstration, main


def program(pid, mode, value, group, priority, cap, region='*'):
    return {
        'id': pid,
        'start': '2026-01-01',
        'end': '2027-01-01',
        'region': region,
        'mode': mode,
        'value': value,
        'income_ceiling': 10**12,
        'min_age': 0,
        'max_age': 240,
        'stack_group': group,
        'priority': priority,
        'family_month_cap': cap,
    }


def document(households, fees, subsidies, enrollments):
    return {
        'version': 1,
        'providers': [{'id': 'p', 'region': 'east', 'capacity': 10, 'weekdays': [0, 1, 2, 3, 4, 5, 6], 'closed': [], 'extra_open': []}],
        'households': households,
        'fees': fees,
        'subsidies': subsidies,
        'enrollments': enrollments,
    }


def fee(amount, refundable=False):
    return {'id': 'care' if not refundable else 'deposit', 'provider': 'p', 'start': '2026-01-01', 'end': '2027-01-01', 'kind': 'monthly', 'amount': amount, 'required': True, 'proration': 'calendar', 'min_age': 0, 'max_age': 240, 'refundable': refundable}


def household(ident, weight, children, region='east'):
    return {'id': ident, 'region': region, 'income_low': 1000000, 'income_high': 1000000, 'weight': weight, 'children': [{'id': child, 'birth': '2024-06-01'} for child in children]}


def enroll(children):
    return [{'id': 'e' + child, 'child': child, 'provider': 'p', 'start': '2026-11-01', 'end': '2026-12-01', 'options': [], 'attendance': []} for child in children]


def substitutes_document():
    subsidies = [program('national', 'percent', 5000, 'main', 5, 10**12), program('backup', 'percent', 3000, 'main', 1, 10**12)]
    return document([household('a', 1, ['a0'])], [fee(30000)], subsidies, enroll(['a0']))


def capped_document():
    subsidies = [program('voucher', 'percent', 10000, 'main', 5, 10000), program('grant', 'flat', 3500, 'side', 1, 10**12)]
    return document([household('fam', 1, ['f0', 'f1'])], [fee(8000)], subsidies, enroll(['f0', 'f1']))


def triple_document():
    subsidies = [
        program('voucher', 'percent', 10000, 'main', 5, 10000),
        program('grant', 'flat', 3500, 'side', 1, 10**12),
        program('bonus', 'percent', 2500, 'extra', 2, 10**12),
    ]
    return document([household('fam', 1, ['f0', 'f1'])], [fee(8000)], subsidies, enroll(['f0', 'f1']))


class ContributionTests(unittest.TestCase):
    def setUp(self):
        self.start = '2026-11-01'
        self.end = '2026-12-01'

    def exact(self, catalog, programs):
        definition = normalize_definition(catalog, {'start': self.start, 'end': self.end, 'programs': programs, 'method': 'exact'})
        return run_study(catalog, definition)

    def sampled(self, catalog, programs, budget, seed=11, tolerance=0, minimum=2):
        definition = normalize_definition(catalog, {'start': self.start, 'end': self.end, 'programs': programs, 'method': 'sampled', 'seed': seed, 'min_permutations': minimum, 'se_tolerance_cents': tolerance})
        return definition, run_study(catalog, definition, budget)

    def test_substitutes_make_naive_sums_distort(self):
        catalog = Catalog(substitutes_document())
        result = self.exact(catalog, ['national', 'backup'])
        report = result['report']
        contributions = {row['program']: row['weighted_contribution_cents'] for row in report['programs']}
        self.assertEqual(contributions, {'national': '10500.000000', 'backup': '4500.000000'})
        shares = {row['program']: row['share_of_weighted_reduction'] for row in report['programs']}
        self.assertEqual(shares['national'], '0.700000000000')
        self.assertEqual(shares['backup'], '0.300000000000')
        naive = report['naive_totals']
        self.assertEqual(naive['weighted_total_reduction_cents'], 15000)
        self.assertEqual(naive['standalone_sum_cents'], 24000)
        self.assertEqual(naive['leave_one_out_sum_cents'], 6000)
        self.assertGreater(naive['standalone_sum_cents'], naive['weighted_total_reduction_cents'])
        self.assertLess(naive['leave_one_out_sum_cents'], naive['weighted_total_reduction_cents'])
        self.assertEqual(report['conservation']['weighted_residual_cents'], '0.000000')
        self.assertEqual(report['conservation']['sample_residual_cents'], '0.000000')
        self.assertEqual(report['interpretation'], 'mechanical_average_marginal_contributions_over_enablement_orders__not_causal_policy_effects')
        self.assertTrue(verify_study(result))
        self.assertTrue(replay_study(catalog, result))

    def test_family_cap_and_siblings_stay_together(self):
        catalog = Catalog(capped_document())
        result = self.exact(catalog, ['voucher', 'grant'])
        report = result['report']
        contributions = {row['program']: row['weighted_contribution_cents'] for row in report['programs']}
        self.assertEqual(contributions, {'voucher': '9500.000000', 'grant': '6500.000000'})
        rows = report['households']
        self.assertEqual(len(rows), 1)
        fam = rows[0]
        self.assertEqual(fam['household'], 'fam')
        self.assertEqual(fam['empty_net_operating_cents'], 16000)
        self.assertEqual(fam['full_net_operating_cents'], 0)
        self.assertEqual(fam['reduction_cents'], 16000)
        voucher = next(row for row in report['programs'] if row['program'] == 'voucher')
        self.assertEqual(voucher['standalone_weighted_reduction_cents'], 10000)
        self.assertLess(voucher['standalone_weighted_reduction_cents'], 16000)
        naive = report['naive_totals']
        self.assertEqual(naive['standalone_sum_cents'], 17000)
        self.assertEqual(naive['leave_one_out_sum_cents'], 15000)
        self.assertEqual(naive['weighted_total_reduction_cents'], 16000)
        self.assertEqual(report['conservation']['weighted_residual_cents'], '0.000000')
        self.assertTrue(replay_study(catalog, result))

    def test_exact_matches_hand_computation(self):
        catalog = Catalog(triple_document())
        result = self.exact(catalog, ['voucher', 'grant', 'bonus'])
        self.assertEqual(result['permutations_completed'], 6)
        self.assertEqual(result['subsets_evaluated'], 8)
        contributions = {row['program']: row['weighted_contribution_cents'] for row in result['report']['programs']}
        self.assertEqual(contributions['voucher'], '8166.666667')
        self.assertEqual(contributions['grant'], '5166.666667')
        self.assertEqual(contributions['bonus'], '2666.666667')
        total = sum(Decimal(value) for value in contributions.values())
        self.assertEqual(total, Decimal('16000.000001'))
        self.assertEqual(result['report']['conservation']['weighted_residual_cents'], '0.000000')
        self.assertEqual(result['report']['naive_totals']['standalone_sum_cents'], 21000)
        self.assertEqual(result['report']['naive_totals']['leave_one_out_sum_cents'], 7000)

    def test_sampled_corresponds_to_exact_at_small_scale(self):
        catalog = Catalog(triple_document())
        programs = ['voucher', 'grant', 'bonus']
        exact = self.exact(catalog, programs)
        exact_values = {row['program']: Decimal(row['weighted_contribution_cents']) for row in exact['report']['programs']}
        definition, sampled = self.sampled(catalog, programs, 120)
        self.assertEqual(sampled['permutations_completed'], 120)
        self.assertEqual(sampled['stopped_by'], 'budget')
        for row in sampled['report']['programs']:
            estimate = Decimal(row['weighted_contribution_cents'])
            error = Decimal(row['standard_error_cents'])
            self.assertLessEqual(abs(estimate - exact_values[row['program']]), 3 * error)
        self.assertEqual(sampled['report']['conservation']['weighted_residual_cents'], '0.000000')
        self.assertEqual(sampled['report']['conservation']['sample_residual_cents'], '0.000000')
        again = run_study(catalog, definition, 120)
        self.assertEqual(again, sampled)
        other_seed = normalize_definition(catalog, {'start': self.start, 'end': self.end, 'programs': programs, 'method': 'sampled', 'seed': 12, 'min_permutations': 2, 'se_tolerance_cents': 0})
        other = run_study(catalog, other_seed, 120)
        self.assertNotEqual(other['study_digest'], sampled['study_digest'])
        self.assertEqual(other['report']['conservation']['weighted_residual_cents'], '0.000000')
        self.assertTrue(replay_study(catalog, sampled))

    def test_standard_error_uses_per_permutation_marginals(self):
        catalog = Catalog(triple_document())
        _, sampled = self.sampled(catalog, ['voucher', 'grant', 'bonus'], 60)
        permutations = sampled['permutations_completed']
        reported = {row['program']: row['standard_error_cents'] for row in sampled['report']['programs']}
        for row in sampled['weighted']:
            total = row['weighted_marginal_sum_cents']
            squares = row['weighted_marginal_sumsq_cents2']
            variance = Fraction(squares, permutations - 1) - Fraction(total * total, permutations * (permutations - 1))
            expected = float(variance / permutations) ** 0.5
            self.assertAlmostEqual(float(reported[row['program']]), expected, places=6)

    def test_resume_extends_along_the_same_stream(self):
        catalog = Catalog(triple_document())
        programs = ['voucher', 'grant', 'bonus']
        definition, first = self.sampled(catalog, programs, 40)
        self.assertEqual(first['permutations_completed'], 40)
        resumed = resume_study(catalog, definition, first, 80)
        direct = run_study(catalog, definition, 120)
        self.assertEqual(resumed, direct)
        self.assertEqual(resumed['permutations_completed'], 120)
        exact_definition = normalize_definition(catalog, {'start': self.start, 'end': self.end, 'programs': programs, 'method': 'exact'})
        exact = run_study(catalog, exact_definition)
        with self.assertRaises(ValueError):
            resume_study(catalog, exact_definition, exact, 5)
        with self.assertRaises(ValueError):
            resume_study(catalog, definition, resumed, MAX_PERMUTATIONS)
        changed = normalize_definition(catalog, {'start': self.start, 'end': self.end, 'programs': programs, 'method': 'sampled', 'seed': 99, 'min_permutations': 2, 'se_tolerance_cents': 0})
        with self.assertRaises(ValueError):
            resume_study(catalog, changed, resumed, 5)

    def test_precision_stopping_rule_is_simultaneous(self):
        catalog = Catalog(triple_document())
        programs = ['voucher', 'grant', 'bonus']
        _, stopped = self.sampled(catalog, programs, 500, tolerance=10**9, minimum=4)
        self.assertEqual(stopped['permutations_completed'], 4)
        self.assertEqual(stopped['stopped_by'], 'precision')
        precision = stopped['report']['precision']
        self.assertTrue(precision['precision_met'])
        self.assertEqual(precision['seed'], 11)
        self.assertEqual(precision['min_permutations'], 4)
        self.assertEqual(precision['se_tolerance_cents'], 10**9)
        _, exhausted = self.sampled(catalog, programs, 30, tolerance=0)
        self.assertEqual(exhausted['permutations_completed'], 30)
        self.assertEqual(exhausted['stopped_by'], 'budget')
        self.assertFalse(exhausted['report']['precision']['precision_met'])
        for row in exhausted['report']['programs']:
            self.assertIsNotNone(row['standard_error_cents'])

    def test_zero_reduction_reports_no_percentages(self):
        data = substitutes_document()
        data['subsidies'] = [program('northern', 'percent', 5000, 'main', 5, 10**12, region='north')]
        catalog = Catalog(data)
        result = self.exact(catalog, ['northern'])
        report = result['report']
        self.assertEqual(report['total_reduction_cents'], 0)
        self.assertEqual(report['weighted_total_reduction_cents'], 0)
        row = report['programs'][0]
        self.assertEqual(row['weighted_contribution_cents'], '0.000000')
        self.assertIsNone(row['share_of_weighted_reduction'])
        self.assertEqual(report['conservation']['weighted_residual_cents'], '0.000000')
        for group in report['by_region'] + report['by_income_band']:
            self.assertEqual(group['weighted_reduction_cents'], 0)
            self.assertEqual(group['contributions'], {'northern': '0.000000'})
            self.assertIsNone(group['shares']['northern'])
        self.assertTrue(verify_study(result))

    def test_background_programs_stay_enabled_and_labeled(self):
        data = json.loads(Path('fixtures/county.json').read_text())
        catalog = Catalog(data)
        definition = normalize_definition(catalog, {'start': self.start, 'end': self.end, 'programs': ['family'], 'method': 'exact'})
        self.assertEqual(definition['background_programs'], ['local'])
        result = run_study(catalog, definition)
        report = result['report']
        self.assertEqual(report['empty_set']['subsidy_cents'], 6000)
        self.assertEqual(report['full_set']['subsidy_cents'], 58400)
        self.assertEqual(report['total_reduction_cents'], 52400)
        self.assertEqual(report['weighted_total_reduction_cents'], 72400)
        self.assertIn('value_function_is_household_month_net_operating_expenditure_with_background_subsidies_always_enabled', report['notes'])
        self.assertTrue(replay_study(catalog, result))

    def test_fixture_household_and_group_breakdown(self):
        data = json.loads(Path('fixtures/county.json').read_text())
        catalog = Catalog(data)
        result = self.exact(catalog, ['family', 'local'])
        report = result['report']
        rows = {row['household']: row for row in report['households']}
        self.assertEqual(rows['h']['contributions'], {'family': '20000.000000', 'local': '6000.000000'})
        self.assertEqual(rows['h']['reduction_cents'], 26000)
        self.assertEqual(rows['h']['empty_deposit_cents'], 10000)
        self.assertEqual(rows['h']['full_deposit_cents'], 10000)
        self.assertEqual(rows['i']['contributions'], {'family': '16200.000000', 'local': '0.000000'})
        self.assertEqual(rows['missing']['contributions'], {'family': '0.000000', 'local': '0.000000'})
        self.assertEqual(rows['nonuser']['reduction_cents'], 0)
        regions = {row['region']: row for row in report['by_region']}
        self.assertEqual(regions['east']['weighted_reduction_cents'], 52000)
        self.assertEqual(regions['east']['contributions'], {'family': '40000.000000', 'local': '12000.000000'})
        self.assertEqual(regions['west']['weighted_reduction_cents'], 32400)
        bands = {row['income_band']: row for row in report['by_income_band']}
        self.assertEqual(bands['low']['weighted_reduction_cents'], 84400)
        self.assertEqual(bands['unknown']['weighted_reduction_cents'], 0)
        self.assertIsNone(bands['unknown']['shares']['family'])
        weighted = sum(row['weighted_reduction_cents'] for row in report['by_region'])
        self.assertEqual(weighted, report['weighted_total_reduction_cents'])
        household_weighted = sum(Decimal(row['contributions']['family']) * row['weight'] for row in report['households'])
        family = next(row for row in report['programs'] if row['program'] == 'family')
        self.assertEqual(household_weighted, Decimal(family['weighted_contribution_cents']))
        self.assertEqual(report['empty_set']['deposit_cents'], 25000)
        self.assertEqual(report['full_set']['deposit_cents'], 25000)
        self.assertTrue(replay_study(catalog, result))

    def test_definition_validation(self):
        catalog = Catalog(substitutes_document())
        with self.assertRaises(ValueError):
            normalize_definition(catalog, {'start': self.start, 'end': self.end, 'programs': ['national', 'national'], 'method': 'exact'})
        with self.assertRaises(ValueError):
            normalize_definition(catalog, {'start': self.start, 'end': self.end, 'programs': [], 'method': 'exact'})
        with self.assertRaises(ValueError):
            normalize_definition(catalog, {'start': self.start, 'end': self.end, 'programs': ['unknown'], 'method': 'exact'})
        with self.assertRaises(ValueError):
            normalize_definition(catalog, {'start': self.start, 'end': self.end, 'programs': ['national'], 'method': 'exact', 'seed': 1})
        with self.assertRaises(ValueError):
            normalize_definition(catalog, {'start': self.start, 'end': self.end, 'programs': ['national'], 'method': 'sampled', 'seed': 1, 'min_permutations': 2})
        with self.assertRaises(ValueError):
            normalize_definition(catalog, {'start': self.start, 'end': self.end, 'programs': ['national'], 'method': 'sampled', 'seed': 1, 'min_permutations': 1, 'se_tolerance_cents': 0})
        with self.assertRaises(ValueError):
            normalize_definition(catalog, {'start': self.start, 'end': self.end, 'programs': ['national'], 'method': 'exact', 'missing_income': 'assume'})
        with self.assertRaises(ValueError):
            normalize_definition(catalog, {'start': self.start, 'end': self.end, 'programs': ['national'], 'method': 'exact', 'assumed_income_cents': 5})
        with self.assertRaises(ValueError):
            normalize_definition(catalog, {'start': self.start, 'end': self.end, 'programs': ['national'], 'method': 'exact', 'extra': 1})
        with self.assertRaises(ValueError):
            normalize_definition(catalog, {'start': self.start, 'end': self.end, 'programs': ['national'], 'method': 'exact', 'income_endpoint': 'middle'})
        many = [program(f'p{index}', 'flat', 100, 'g' + str(index), 1, 10**12) for index in range(13)]
        data = substitutes_document()
        data['subsidies'] = many
        with self.assertRaises(ValueError):
            normalize_definition(Catalog(data), {'start': self.start, 'end': self.end, 'programs': [row['id'] for row in many], 'method': 'sampled', 'seed': 1, 'min_permutations': 2, 'se_tolerance_cents': 0})
        nine = many[:9]
        data['subsidies'] = nine
        with self.assertRaises(ValueError):
            normalize_definition(Catalog(data), {'start': self.start, 'end': self.end, 'programs': [row['id'] for row in nine], 'method': 'exact'})

    def test_verify_detects_tampering(self):
        catalog = Catalog(triple_document())
        definition, sampled = self.sampled(catalog, ['voucher', 'grant', 'bonus'], 20)
        mutated = copy.deepcopy(sampled)
        mutated['contributions'][0]['marginal_sum_cents'] += 1
        with self.assertRaises(ValueError):
            verify_study(mutated)
        mutated = copy.deepcopy(sampled)
        mutated['report']['programs'][0]['weighted_contribution_cents'] = '1.000000'
        with self.assertRaises(ValueError):
            verify_study(mutated)
        mutated = copy.deepcopy(sampled)
        mutated['stopped_by'] = 'precision'
        with self.assertRaises(ValueError):
            verify_study(mutated)
        mutated = copy.deepcopy(sampled)
        mutated['weighted'][0]['weighted_marginal_sumsq_cents2'] += 1
        with self.assertRaises(ValueError):
            verify_study(mutated)
        exact = self.exact(catalog, ['voucher', 'grant', 'bonus'])
        mutated = copy.deepcopy(exact)
        mutated['cells'][0]['full_net_operating_cents'] -= 1
        with self.assertRaises(ValueError):
            verify_study(mutated)
        with self.assertRaises(ValueError):
            replay_study(catalog, mutated)

    def test_income_policy_is_part_of_the_definition(self):
        data = json.loads(Path('fixtures/county.json').read_text())
        catalog = Catalog(data)
        assumed = normalize_definition(catalog, {'start': self.start, 'end': self.end, 'programs': ['family', 'local'], 'method': 'exact', 'missing_income': 'assume', 'assumed_income_cents': 1200000})
        self.assertEqual(assumed['missing_income'], 'assume')
        self.assertEqual(assumed['assumed_income_cents'], 1200000)
        result = run_study(catalog, assumed)
        rows = {row['household']: row for row in result['report']['households']}
        self.assertEqual(rows['missing']['reduction_cents'], 19200)
        excluded = self.exact(catalog, ['family', 'local'])
        excluded_rows = {row['household']: row for row in excluded['report']['households']}
        self.assertEqual(excluded_rows['missing']['reduction_cents'], 0)
        self.assertNotEqual(result['definition_digest'], excluded['definition_digest'])

    def test_archive_lifecycle_and_immutability(self):
        data = triple_document()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'archive.sqlite3'
            with Archive(path) as archive:
                archive.add_input('county', data)
                service = Service(archive)
                definition = {'start': self.start, 'end': self.end, 'programs': ['voucher', 'grant', 'bonus'], 'method': 'sampled', 'seed': 11, 'min_permutations': 2, 'se_tolerance_cents': 0}
                created = service.dispatch('study-create', {'id': 'study', 'input': 'county', 'definition': definition, 'max_permutations': 40})
                self.assertEqual(created['result']['permutations_completed'], 40)
                again = service.dispatch('study-create', {'id': 'study', 'input': 'county', 'definition': definition, 'max_permutations': 40})
                self.assertEqual(again['revision'], 1)
                changed = dict(definition, seed=12)
                with self.assertRaises(Conflict):
                    service.dispatch('study-create', {'id': 'study', 'input': 'county', 'definition': changed, 'max_permutations': 40})
                fetched = service.dispatch('study-get', {'id': 'study'})
                self.assertEqual(fetched['definition']['seed'], 11)
                resumed = service.dispatch('study-resume', {'id': 'study', 'additional_permutations': 30})
                self.assertEqual(resumed['result']['permutations_completed'], 70)
                self.assertEqual(resumed['revision'], 2)
                self.assertEqual(service.dispatch('list', {'table': 'studies'})['ids'], ['study'])
                self.assertTrue(recovery.audit(archive)['valid'])
                exact_definition = {'start': self.start, 'end': self.end, 'programs': ['voucher', 'grant'], 'method': 'exact'}
                service.dispatch('study-create', {'id': 'exact-study', 'input': 'county', 'definition': exact_definition})
                with self.assertRaises(Conflict):
                    service.dispatch('study-resume', {'id': 'exact-study', 'additional_permutations': 5})
                self.assertEqual(recovery.audit(archive)['studies'], 2)
                evidence = recovery.backup(archive, Path(directory) / 'backup.sqlite3')
            recovery.restore(Path(directory) / 'backup.sqlite3', Path(directory) / 'restored.sqlite3', evidence['digest'])
            with Archive(Path(directory) / 'restored.sqlite3') as restored:
                self.assertTrue(recovery.audit(restored)['valid'])
                study = restored.study('study')
                self.assertEqual(study['result']['permutations_completed'], 70)
                archive_row = restored.db.execute('SELECT result FROM studies WHERE id=?', ('study',)).fetchone()
                payload = json.loads(archive_row['result'])
                payload['contributions'][0]['marginal_sum_cents'] += 1
                restored.db.execute('UPDATE studies SET result=? WHERE id=?', (json.dumps(payload), 'study'))
                with self.assertRaises(ValueError):
                    restored.study('study')

    def test_cli_request_passthrough(self):
        from contextlib import redirect_stdout
        from io import StringIO
        data = substitutes_document()
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / 'a.sqlite3')
            input_body = Path(directory) / 'input.json'
            input_body.write_text(json.dumps({'id': 'county', 'document': data}))
            study_body = Path(directory) / 'study.json'
            study_body.write_text(json.dumps({'id': 's', 'input': 'county', 'definition': {'start': self.start, 'end': self.end, 'programs': ['national', 'backup'], 'method': 'exact'}}))
            get_body = Path(directory) / 'get.json'
            get_body.write_text(json.dumps({'id': 's'}))
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(['request', '--database', database, '--action', 'input', '--body', str(input_body)]), 0)
                self.assertEqual(main(['request', '--database', database, '--action', 'study-create', '--body', str(study_body)]), 0)
                self.assertEqual(main(['request', '--database', database, '--action', 'study-get', '--body', str(get_body)]), 0)
            fetched = json.loads(output.getvalue().strip().splitlines()[-1])
            self.assertEqual(fetched['result']['report']['weighted_total_reduction_cents'], 15000)

    def test_permutation_stream_is_complete_and_stable(self):
        first = permutation_stream(5, ['a', 'b', 'c'])
        second = permutation_stream(5, ['a', 'b', 'c'])
        for _ in range(20):
            permutation = next(first)
            self.assertEqual(sorted(permutation), ['a', 'b', 'c'])
            self.assertEqual(permutation, next(second))

    def test_demo_includes_contribution_study(self):
        result = demonstration('fixtures/county.json', self.start, self.end)
        study = result['contribution_study']
        self.assertEqual(study['weighted_total_reduction_cents'], 84400)
        self.assertEqual(study['conservation']['weighted_residual_cents'], '0.000000')
        self.assertEqual(result['archive']['studies'], 1)


if __name__ == '__main__':
    unittest.main()
