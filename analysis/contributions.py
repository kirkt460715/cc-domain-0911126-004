from collections import defaultdict
from decimal import Decimal, localcontext
from fractions import Fraction
from math import factorial
import random

from tariffs.primitives import days, digest, integer, word
from analysis.engine import evaluate
from analysis.policy_review import remove_programs

EXACT_PROGRAM_LIMIT = 8
SAMPLED_PROGRAM_LIMIT = 12
MAX_PERMUTATIONS = 5000
CENT = '.6f'
RATIO = '.12f'

INTERPRETATION = 'mechanical_average_marginal_contributions_over_enablement_orders__not_causal_policy_effects'

BASE_NOTES = [
    'value_function_is_household_month_net_operating_expenditure_with_background_subsidies_always_enabled',
    'deposits_are_excluded_from_the_value_function_and_listed_separately',
    'household_contributions_are_computed_before_weighting__siblings_are_never_split_into_separate_households',
    'positive_and_negative_contributions_are_reported_as_computed',
    'shares_are_null_when_the_reduction_denominator_is_zero',
]
EXACT_NOTE = 'all_enablement_orders_are_enumerated_exactly__standard_error_is_exactly_zero'
SAMPLED_NOTE = 'every_permutation_is_a_complete_ordering__estimates_are_never_rescaled_to_force_conservation'


def render_fraction(value, places=CENT):
    with localcontext() as context:
        context.prec = 70
        return format(Decimal(value.numerator) / Decimal(value.denominator), places)


def render_root(value, places=CENT):
    with localcontext() as context:
        context.prec = 70
        root = (Decimal(value.numerator) / Decimal(value.denominator)).sqrt()
        return format(root, places)


def normalize_definition(catalog, definition):
    if not isinstance(definition, dict):
        raise ValueError('study definition must be an object')
    allowed = {'start', 'end', 'income_endpoint', 'missing_income', 'assumed_income_cents', 'programs', 'method', 'seed', 'min_permutations', 'se_tolerance_cents'}
    if set(definition) - allowed:
        raise ValueError('unexpected study definition field')
    if not {'start', 'end', 'programs', 'method'} <= set(definition):
        raise ValueError('study definition requires start, end, programs, method')
    start, end = definition['start'], definition['end']
    days(start, end)
    endpoint = definition.get('income_endpoint', 'low')
    missing = definition.get('missing_income', 'exclude')
    assumed = definition.get('assumed_income_cents')
    if endpoint not in ('low', 'high') or missing not in ('exclude', 'assume'):
        raise ValueError('invalid income policy')
    if missing == 'assume':
        integer(assumed, 'assumed_income_cents')
    elif assumed is not None:
        raise ValueError('assumed income only permitted with assume mode')
    programs = definition['programs']
    if not isinstance(programs, list) or not programs or len(programs) != len(set(programs)):
        raise ValueError('expected a nonempty list of unique programs')
    if len(programs) > SAMPLED_PROGRAM_LIMIT:
        raise ValueError('too many selected programs')
    for program in programs:
        word(program, 'programs')
    if set(programs) - catalog.subsidies.keys():
        raise ValueError('unknown selected program')
    method = definition['method']
    if method not in ('exact', 'sampled'):
        raise ValueError('unknown contribution method')
    normalized = {
        'input_digest': catalog.fingerprint,
        'start': start,
        'end': end,
        'income_endpoint': endpoint,
        'missing_income': missing,
        'assumed_income_cents': assumed,
        'selected_programs': sorted(programs),
        'background_programs': sorted(set(catalog.subsidies) - set(programs)),
        'method': method,
    }
    if method == 'exact':
        if len(programs) > EXACT_PROGRAM_LIMIT:
            raise ValueError('exact method is limited to small program sets')
        if {'seed', 'min_permutations', 'se_tolerance_cents'} & set(definition):
            raise ValueError('exact method takes no sampling parameters')
    else:
        if not {'seed', 'min_permutations', 'se_tolerance_cents'} <= set(definition):
            raise ValueError('sampled method requires seed, min_permutations and se_tolerance_cents')
        integer(definition['seed'], 'seed', 0)
        integer(definition['min_permutations'], 'min_permutations', 2, MAX_PERMUTATIONS)
        integer(definition['se_tolerance_cents'], 'se_tolerance_cents', 0)
        normalized['seed'] = definition['seed']
        normalized['min_permutations'] = definition['min_permutations']
        normalized['se_tolerance_cents'] = definition['se_tolerance_cents']
    return normalized


class SubsetEvaluator:
    def __init__(self, catalog, definition):
        self.catalog = catalog
        self.definition = definition
        self.cache = {}
        self.meta = None
        self.evaluations = 0

    def values(self, kept):
        key = frozenset(kept)
        if key not in self.cache:
            dropped = [program for program in self.definition['selected_programs'] if program not in key]
            variant = remove_programs(self.catalog, dropped)
            result = evaluate(variant, self.definition['start'], self.definition['end'], self.definition['income_endpoint'], self.definition['missing_income'], self.definition['assumed_income_cents'])
            if self.meta is None:
                self.meta = {row['household']: {'weight': row['weight'], 'region': row['region'], 'income_band': row['income_band']} for row in result['families']}
            self.cache[key] = {(row['household'], row['month']): (row['net_operating_cents'], row['deposit_cents'], row['subsidy_cents']) for row in result['families']}
            self.evaluations += 1
        return self.cache[key]


def permutation_stream(seed, programs):
    rng = random.Random(seed)
    ordered = list(programs)
    while True:
        permutation = list(ordered)
        # Fisher-Yates driven only by random(), whose sequence is stable across versions.
        for index in range(len(permutation) - 1, 0, -1):
            swap = int(rng.random() * (index + 1))
            permutation[index], permutation[swap] = permutation[swap], permutation[index]
        yield permutation


class Accumulator:
    def __init__(self, programs, cells, weights):
        self.programs = list(programs)
        self.weights = weights
        self.permutations = 0
        self.cell_sums = {program: {cell: 0 for cell in cells} for program in self.programs}
        self.weighted_sums = {program: 0 for program in self.programs}
        self.weighted_sumsqs = {program: 0 for program in self.programs}

    def add_permutation(self, marginals):
        for program in self.programs:
            weighted = 0
            for cell, marginal in marginals[program].items():
                self.cell_sums[program][cell] += marginal
                weighted += self.weights[cell[0]] * marginal
            self.weighted_sums[program] += weighted
            self.weighted_sumsqs[program] += weighted * weighted
        self.permutations += 1

    def refresh_weighted(self):
        for program in self.programs:
            self.weighted_sums[program] = sum(self.weights[cell[0]] * marginal for cell, marginal in self.cell_sums[program].items())


def mean_variances(weighted_rows, permutations):
    result = {}
    for row in weighted_rows:
        if permutations < 2:
            result[row['program']] = None
            continue
        total = row['weighted_marginal_sum_cents']
        squares = row['weighted_marginal_sumsq_cents2']
        sample_variance = Fraction(squares, permutations - 1) - Fraction(total * total, permutations * (permutations - 1))
        result[row['program']] = sample_variance / permutations
    return result


def precision_reached(weighted_rows, permutations, tolerance_cents):
    if permutations < 2:
        return False
    limit = Fraction(tolerance_cents) ** 2
    for variance in mean_variances(weighted_rows, permutations).values():
        if variance is None or variance > limit:
            return False
    return True


def prepare(catalog, definition):
    if definition.get('input_digest') != catalog.fingerprint:
        raise ValueError('study definition belongs to another input')
    evaluator = SubsetEvaluator(catalog, definition)
    empty = evaluator.values(frozenset())
    cells = sorted(empty)
    weights = {household: row['weight'] for household, row in evaluator.meta.items()}
    return evaluator, cells, weights


def run_exact(catalog, definition):
    programs = definition['selected_programs']
    evaluator, cells, weights = prepare(catalog, definition)
    accumulator = Accumulator(programs, cells, weights)
    count = len(programs)
    for mask in range(1 << count):
        subset = {programs[index] for index in range(count) if mask & (1 << index)}
        base = evaluator.values(subset)
        for program in programs:
            if program in subset:
                continue
            factor = factorial(len(subset)) * factorial(count - len(subset) - 1)
            extended = evaluator.values(subset | {program})
            for cell in cells:
                accumulator.cell_sums[program][cell] += factor * (base[cell][0] - extended[cell][0])
    accumulator.permutations = factorial(count)
    accumulator.refresh_weighted()
    return evaluator, accumulator


def run_sampled(catalog, definition, budget, accumulator=None, skip=0, early_stop=True):
    programs = definition['selected_programs']
    evaluator, cells, weights = prepare(catalog, definition)
    if accumulator is None:
        accumulator = Accumulator(programs, cells, weights)
    empty = evaluator.values(frozenset())
    stream = permutation_stream(definition['seed'], programs)
    for _ in range(skip):
        next(stream)
    stopped_by = 'budget'
    added = 0
    while added < budget:
        permutation = next(stream)
        previous = empty
        kept = frozenset()
        marginals = {}
        for program in permutation:
            kept = kept | {program}
            current = evaluator.values(kept)
            marginals[program] = {cell: previous[cell][0] - current[cell][0] for cell in cells}
            previous = current
        accumulator.add_permutation(marginals)
        added += 1
        if early_stop and accumulator.permutations >= definition['min_permutations']:
            rows = [{'program': program, 'weighted_marginal_sum_cents': accumulator.weighted_sums[program], 'weighted_marginal_sumsq_cents2': accumulator.weighted_sumsqs[program]} for program in programs]
            if precision_reached(rows, accumulator.permutations, definition['se_tolerance_cents']):
                stopped_by = 'precision'
                break
    return evaluator, accumulator, stopped_by


def build_result(evaluator, definition, accumulator, stopped_by):
    programs = definition['selected_programs']
    sampled = definition['method'] == 'sampled'
    empty = evaluator.values(frozenset())
    full = evaluator.values(frozenset(programs))
    meta = evaluator.meta
    cells = []
    for household, month in sorted(empty):
        before, after = empty[(household, month)], full[(household, month)]
        cells.append({
            'household': household,
            'month': month,
            'empty_net_operating_cents': before[0],
            'full_net_operating_cents': after[0],
            'empty_deposit_cents': before[1],
            'full_deposit_cents': after[1],
            'empty_subsidy_cents': before[2],
            'full_subsidy_cents': after[2],
        })
    weighted_rows = []
    for program in programs:
        alone = evaluator.values(frozenset({program}))
        without = evaluator.values(frozenset(item for item in programs if item != program))
        standalone = sum(meta[household]['weight'] * (empty[cell][0] - alone[cell][0]) for cell in empty)
        leave_one_out = sum(meta[household]['weight'] * (without[cell][0] - full[cell][0]) for cell in empty)
        row = {
            'program': program,
            'weighted_marginal_sum_cents': accumulator.weighted_sums[program],
            'standalone_reduction_cents': standalone,
            'leave_one_out_reduction_cents': leave_one_out,
        }
        if sampled:
            row['weighted_marginal_sumsq_cents2'] = accumulator.weighted_sumsqs[program]
        weighted_rows.append(row)
    contributions = [{'program': program, 'household': household, 'month': month, 'marginal_sum_cents': accumulator.cell_sums[program][(household, month)]} for program in programs for household, month in sorted(empty)]
    households = [{'household': household, 'region': meta[household]['region'], 'income_band': meta[household]['income_band'], 'weight': meta[household]['weight']} for household in sorted(meta)]
    report = render_report(definition, households, cells, contributions, weighted_rows, accumulator.permutations, stopped_by)
    body = {
        'version': 1,
        'kind': 'subsidy_contribution_study',
        'definition': definition,
        'definition_digest': digest(definition),
        'method': definition['method'],
        'permutations_completed': accumulator.permutations,
        'subsets_evaluated': evaluator.evaluations,
        'households': households,
        'cells': cells,
        'contributions': contributions,
        'weighted': weighted_rows,
        'report': report,
    }
    if sampled:
        body['stopped_by'] = stopped_by
    return {**body, 'study_digest': digest(body)}


def render_report(definition, households, cells, contributions, weighted, permutations, stopped_by):
    programs = definition['selected_programs']
    sampled = definition['method'] == 'sampled'
    meta = {row['household']: row for row in households}
    weights = {row['household']: row['weight'] for row in households}
    cell_sums = {(row['program'], row['household'], row['month']): row['marginal_sum_cents'] for row in contributions}
    weighted_rows = {row['program']: row for row in weighted}

    def weighted_total(key):
        return sum(row[key] * weights[row['household']] for row in cells)

    totals = {}
    for prefix in ('empty', 'full'):
        totals[prefix] = {
            'net_operating_cents': sum(row[f'{prefix}_net_operating_cents'] for row in cells),
            'deposit_cents': sum(row[f'{prefix}_deposit_cents'] for row in cells),
            'subsidy_cents': sum(row[f'{prefix}_subsidy_cents'] for row in cells),
            'weighted_net_operating_cents': weighted_total(f'{prefix}_net_operating_cents'),
            'weighted_deposit_cents': weighted_total(f'{prefix}_deposit_cents'),
            'weighted_subsidy_cents': weighted_total(f'{prefix}_subsidy_cents'),
        }
    reduction = totals['empty']['net_operating_cents'] - totals['full']['net_operating_cents']
    weighted_reduction = totals['empty']['weighted_net_operating_cents'] - totals['full']['weighted_net_operating_cents']
    variances = mean_variances(weighted, permutations) if sampled else {}
    program_rows = []
    for program in programs:
        row = weighted_rows[program]
        weighted_contribution = Fraction(row['weighted_marginal_sum_cents'], permutations)
        sample_contribution = Fraction(sum(cell_sums[(program, cell['household'], cell['month'])] for cell in cells), permutations)
        if sampled:
            variance = variances[program]
            error = render_root(variance) if variance is not None else None
        else:
            error = render_fraction(Fraction(0))
        program_rows.append({
            'program': program,
            'sample_contribution_cents': render_fraction(sample_contribution),
            'weighted_contribution_cents': render_fraction(weighted_contribution),
            'share_of_weighted_reduction': render_fraction(weighted_contribution / weighted_reduction, RATIO) if weighted_reduction else None,
            'standard_error_cents': error,
            'standalone_weighted_reduction_cents': row['standalone_reduction_cents'],
            'leave_one_out_weighted_reduction_cents': row['leave_one_out_reduction_cents'],
        })
    contributions_sum = sum(Fraction(row['weighted_marginal_sum_cents'], permutations) for row in weighted)
    sample_sum = sum(Fraction(sum(cell_sums[(program, cell['household'], cell['month'])] for cell in cells), permutations) for program in programs)
    groups = {}
    for dimension in ('region', 'income_band'):
        bucket = defaultdict(list)
        for cell in cells:
            bucket[meta[cell['household']][dimension]].append(cell)
        rows = []
        for key in sorted(bucket):
            members = bucket[key]
            group_empty = sum(cell['empty_net_operating_cents'] * weights[cell['household']] for cell in members)
            group_full = sum(cell['full_net_operating_cents'] * weights[cell['household']] for cell in members)
            group_reduction = group_empty - group_full
            group_contributions = {}
            group_shares = {}
            for program in programs:
                contribution = Fraction(sum(weights[cell['household']] * cell_sums[(program, cell['household'], cell['month'])] for cell in members), permutations)
                group_contributions[program] = render_fraction(contribution)
                group_shares[program] = render_fraction(contribution / group_reduction, RATIO) if group_reduction else None
            rows.append({
                dimension: key,
                'household_months': len(members),
                'weight': sum(weights[cell['household']] for cell in members),
                'weighted_empty_net_cents': group_empty,
                'weighted_full_net_cents': group_full,
                'weighted_reduction_cents': group_reduction,
                'contributions': group_contributions,
                'shares': group_shares,
            })
        groups[dimension] = rows
    family_rows = []
    for cell in cells:
        household, month = cell['household'], cell['month']
        family_rows.append({
            'household': household,
            'month': month,
            'region': meta[household]['region'],
            'income_band': meta[household]['income_band'],
            'weight': weights[household],
            'empty_net_operating_cents': cell['empty_net_operating_cents'],
            'full_net_operating_cents': cell['full_net_operating_cents'],
            'reduction_cents': cell['empty_net_operating_cents'] - cell['full_net_operating_cents'],
            'empty_deposit_cents': cell['empty_deposit_cents'],
            'full_deposit_cents': cell['full_deposit_cents'],
            'empty_subsidy_cents': cell['empty_subsidy_cents'],
            'full_subsidy_cents': cell['full_subsidy_cents'],
            'contributions': {program: render_fraction(Fraction(cell_sums[(program, household, month)], permutations)) for program in programs},
        })
    report = {
        'empty_set': totals['empty'],
        'full_set': totals['full'],
        'total_reduction_cents': reduction,
        'weighted_total_reduction_cents': weighted_reduction,
        'programs': program_rows,
        'naive_totals': {
            'standalone_sum_cents': sum(row['standalone_reduction_cents'] for row in weighted),
            'leave_one_out_sum_cents': sum(row['leave_one_out_reduction_cents'] for row in weighted),
            'weighted_total_reduction_cents': weighted_reduction,
            'note': 'single_removals_and_standalone_effects_are_not_additive_attribution_when_programs_interact',
        },
        'conservation': {
            'weighted_contributions_sum_cents': render_fraction(contributions_sum),
            'weighted_total_reduction_cents': weighted_reduction,
            'weighted_residual_cents': render_fraction(Fraction(weighted_reduction) - contributions_sum),
            'sample_residual_cents': render_fraction(Fraction(reduction) - sample_sum),
        },
        'by_region': groups['region'],
        'by_income_band': groups['income_band'],
        'households': family_rows,
        'interpretation': INTERPRETATION,
        'notes': BASE_NOTES + [SAMPLED_NOTE if sampled else EXACT_NOTE],
    }
    if sampled:
        met = permutations >= definition['min_permutations'] and precision_reached(weighted, permutations, definition['se_tolerance_cents'])
        report['precision'] = {
            'seed': definition['seed'],
            'min_permutations': definition['min_permutations'],
            'se_tolerance_cents': definition['se_tolerance_cents'],
            'permutations_completed': permutations,
            'precision_met': met,
            'stopped_by': stopped_by,
        }
    return report


def run_study(catalog, definition, max_permutations=None):
    if definition['method'] == 'exact':
        if max_permutations is not None:
            raise ValueError('exact studies take no permutation budget')
        evaluator, accumulator = run_exact(catalog, definition)
        return build_result(evaluator, definition, accumulator, None)
    integer(max_permutations, 'max_permutations', 1, MAX_PERMUTATIONS)
    evaluator, accumulator, stopped_by = run_sampled(catalog, definition, max_permutations)
    return build_result(evaluator, definition, accumulator, stopped_by)


def resume_study(catalog, definition, previous, additional_permutations):
    verify_study(previous)
    if previous['definition'] != definition:
        raise ValueError('resume requires the archived study definition')
    if definition['method'] != 'sampled':
        raise ValueError('only sampled contribution studies can be extended')
    integer(additional_permutations, 'additional_permutations', 1)
    completed = previous['permutations_completed']
    if completed + additional_permutations > MAX_PERMUTATIONS:
        raise ValueError('permutation ceiling reached; archive a new study to continue')
    cells = [(row['household'], row['month']) for row in previous['cells']]
    weights = {row['household']: row['weight'] for row in previous['households']}
    accumulator = Accumulator(definition['selected_programs'], cells, weights)
    accumulator.permutations = completed
    for row in previous['contributions']:
        accumulator.cell_sums[row['program']][(row['household'], row['month'])] = row['marginal_sum_cents']
    for row in previous['weighted']:
        accumulator.weighted_sums[row['program']] = row['weighted_marginal_sum_cents']
        accumulator.weighted_sumsqs[row['program']] = row['weighted_marginal_sumsq_cents2']
    evaluator, accumulator, _ = run_sampled(catalog, definition, additional_permutations, accumulator=accumulator, skip=completed, early_stop=False)
    return build_result(evaluator, definition, accumulator, 'budget')


def verify_study(result):
    body = {key: value for key, value in result.items() if key != 'study_digest'}
    if result.get('study_digest') != digest(body):
        raise ValueError('contribution study digest mismatch')
    definition = result['definition']
    if result['definition_digest'] != digest(definition):
        raise ValueError('contribution study definition digest mismatch')
    programs = definition['selected_programs']
    sampled = definition['method'] == 'sampled'
    permutations = result['permutations_completed']
    integer(permutations, 'permutations_completed', 1)
    integer(result['subsets_evaluated'], 'subsets_evaluated', 1)
    if sampled:
        if permutations > MAX_PERMUTATIONS:
            raise ValueError('permutation ceiling exceeded')
        if result.get('stopped_by') not in ('precision', 'budget'):
            raise ValueError('invalid stop reason')
    else:
        if 'stopped_by' in result:
            raise ValueError('exact study carries no stop reason')
        if permutations != factorial(len(programs)):
            raise ValueError('exact study must enumerate all orderings')
    weights = {}
    for row in result['households']:
        integer(row['weight'], 'weight', 1)
        weights[row['household']] = row['weight']
    cells = {}
    for row in result['cells']:
        key = row['household'], row['month']
        if key in cells or row['household'] not in weights:
            raise ValueError('invalid study cell')
        cells[key] = row
    sums = {program: {} for program in programs}
    for row in result['contributions']:
        if row['program'] not in sums:
            raise ValueError('unknown contribution program')
        key = row['household'], row['month']
        if key not in cells or key in sums[row['program']]:
            raise ValueError('invalid contribution cell')
        integer(row['marginal_sum_cents'], 'marginal_sum_cents', -10**18, 10**18)
        sums[row['program']][key] = row['marginal_sum_cents']
    for program in programs:
        if set(sums[program]) != set(cells):
            raise ValueError('contribution cells incomplete')
    for key, row in cells.items():
        reduction = row['empty_net_operating_cents'] - row['full_net_operating_cents']
        if sum(sums[program][key] for program in programs) != permutations * reduction:
            raise ValueError('contribution conservation failed')
    weighted = {row['program']: row for row in result['weighted']}
    if set(weighted) != set(programs):
        raise ValueError('weighted rows mismatch')
    for program in programs:
        row = weighted[program]
        expected = sum(weights[cell[0]] * sums[program][cell] for cell in cells)
        if row['weighted_marginal_sum_cents'] != expected:
            raise ValueError('weighted contribution mismatch')
        integer(row['standalone_reduction_cents'], 'standalone_reduction_cents', 0)
        integer(row['leave_one_out_reduction_cents'], 'leave_one_out_reduction_cents', 0)
        if sampled:
            integer(row['weighted_marginal_sumsq_cents2'], 'weighted_marginal_sumsq_cents2', 0, 10**40)
        elif 'weighted_marginal_sumsq_cents2' in row:
            raise ValueError('exact study carries no sampling moments')
    expected = render_report(definition, result['households'], result['cells'], result['contributions'], result['weighted'], permutations, result.get('stopped_by'))
    if result['report'] != expected:
        raise ValueError('study report does not re-derive')
    if sampled and result['stopped_by'] == 'precision' and not result['report']['precision']['precision_met']:
        raise ValueError('precision stop without met target')
    return True


def replay_study(catalog, stored):
    verify_study(stored)
    definition = stored['definition']
    if definition['input_digest'] != catalog.fingerprint:
        raise ValueError('study belongs to another input')
    if definition['method'] == 'exact':
        evaluator, accumulator = run_exact(catalog, definition)
        rebuilt = build_result(evaluator, definition, accumulator, None)
    else:
        evaluator, accumulator, _ = run_sampled(catalog, definition, stored['permutations_completed'], early_stop=False)
        rebuilt = build_result(evaluator, definition, accumulator, stored['stopped_by'])
    if rebuilt != stored:
        raise ValueError('contribution study replay mismatch')
    return True
