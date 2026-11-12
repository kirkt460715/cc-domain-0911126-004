from collections import defaultdict
from decimal import Decimal
from analysis.engine import verify
from analysis.weighted import quantile, display, mean, effective_sample_size


def lorenz(result, metric='net_operating_cents', population='enrolled'):
    verify(result)
    if metric not in ('net_operating_cents', 'operating_cents', 'subsidy_cents', 'cash_outlay_cents'):
        raise ValueError('unsupported Lorenz measure')
    if population not in ('enrolled', 'all'):
        raise ValueError('unsupported population')
    rows = [row for row in result['families'] if population == 'all' or row['children_enrolled']]
    rows.sort(key=lambda row: (row[metric], row['household'], row['month']))
    total_weight = sum(row['weight'] for row in rows)
    total_amount = sum(row[metric] * row['weight'] for row in rows)
    points = [{'population_share': '0.000000000000', 'amount_share': '0.000000000000'}]
    weight = 0
    amount = 0
    for row in rows:
        weight += row['weight']
        amount += row[metric] * row['weight']
        points.append({
            'population_share': display(Decimal(weight) / total_weight),
            'amount_share': display(Decimal(amount) / total_amount) if total_amount else None,
        })
    return {
        'result_digest': result['result_digest'],
        'metric': metric,
        'population': population,
        'weighted_total_cents': total_amount,
        'points': points,
        'observation_unit': 'household_month',
    }


def concentration(result):
    verify(result)
    rows = [row for row in result['families'] if row['annual_income_cents'] is not None and row['children_enrolled']]
    rows.sort(key=lambda row: (row['annual_income_cents'], row['household'], row['month']))
    groups = defaultdict(list)
    for row in rows:
        groups[row['annual_income_cents']].append(row)
    weight_total = sum(row['weight'] for row in rows)
    benefit_total = sum(row['subsidy_cents'] * row['weight'] for row in rows)
    points = []
    weight = 0
    benefit = 0
    for income, tied in sorted(groups.items()):
        weight += sum(row['weight'] for row in tied)
        benefit += sum(row['subsidy_cents'] * row['weight'] for row in tied)
        points.append({
            'annual_income_cents': income,
            'cumulative_weight': weight,
            'cumulative_benefit_cents': benefit,
            'population_share': display(Decimal(weight) / weight_total),
            'benefit_share': display(Decimal(benefit) / benefit_total) if benefit_total else None,
        })
    return {
        'result_digest': result['result_digest'],
        'points': points,
        'known_income_weight': weight_total,
        'weighted_subsidy_cents': benefit_total,
        'ties': 'equal_income_rows_collapsed_before_cumulative_shares',
        'missing_income_excluded': True,
    }


def burden_bands(result, boundaries=None):
    verify(result)
    boundaries = boundaries or ['0', '0.05', '0.1', '0.2', '0.4']
    cuts = [Decimal(str(value)) for value in boundaries]
    if any(not value.is_finite() or value < 0 for value in cuts):
        raise ValueError('band cuts must be finite nonnegative ratios')
    if cuts != sorted(set(cuts)) or not cuts or cuts[0] != 0:
        raise ValueError('band cuts must increase strictly from zero')
    groups = defaultdict(list)
    for row in result['families']:
        if not row['children_enrolled']:
            continue
        if row['income_missing']:
            key = 'unknown_income'
        elif row['period_income_cents'] == 0:
            key = 'zero_income_positive_cost' if row['net_operating_cents'] else 'zero_income_zero_cost'
        else:
            value = Decimal(row['burden'])
            key = max(index for index, cut in enumerate(cuts) if value >= cut)
        groups[key].append(row)
    output = []
    for key, rows in sorted(groups.items(), key=lambda pair: str(pair[0])):
        output.append({
            'band': key,
            'minimum': display(cuts[key]) if type(key) is int else None,
            'maximum_exclusive': display(cuts[key + 1]) if type(key) is int and key + 1 < len(cuts) else None,
            'household_months': len(rows),
            'distinct_households': len({row['household'] for row in rows}),
            'weight': sum(row['weight'] for row in rows),
            'weighted_net_cents': sum(row['net_operating_cents'] * row['weight'] for row in rows),
        })
    return output


def region_gaps(result):
    verify(result)
    groups = defaultdict(list)
    for row in result['families']:
        if row['children_enrolled']:
            groups[row['region']].append(row)
    rows = []
    national = [(row['burden'], row['weight']) for values in groups.values() for row in values if row['burden'] is not None]
    reference = mean(national)
    for region, values in sorted(groups.items()):
        valid = [(row['burden'], row['weight']) for row in values if row['burden'] is not None]
        average = mean(valid)
        rows.append({
            'region': region,
            'mean_burden': display(average),
            'national_mean_burden': display(reference),
            'mean_gap': display(average - reference) if average is not None and reference is not None else None,
            'median_burden': display(quantile(valid, '.5')),
            'effective_sample_size': display(effective_sample_size([weight for value, weight in valid])),
            'known_ratio_household_months': len(valid),
            'missing_or_zero_income_household_months': len(values) - len(valid),
            'interpretation': 'descriptive_sample_gap_without_causal_or_population_inference',
        })
    return rows
