from collections import defaultdict
from decimal import Decimal
from analysis.engine import verify
from analysis.weighted import summary, threshold_share, trimmed_mean, display, gini


DIMENSIONS = {'month', 'region', 'income_band'}


def grouped(result, dimensions, population='enrolled', burden_threshold='0.1'):
    verify(result)
    if not isinstance(dimensions, list) or len(dimensions) != len(set(dimensions)) or set(dimensions) - DIMENSIONS:
        raise ValueError('invalid grouping dimensions')
    if population not in ('enrolled', 'all'):
        raise ValueError('population must be enrolled or all')
    threshold = Decimal(str(burden_threshold))
    if not threshold.is_finite() or threshold < 0:
        raise ValueError('burden threshold must be finite and nonnegative')
    groups = defaultdict(list)
    for row in result['families']:
        if population == 'enrolled' and not row['children_enrolled']:
            continue
        groups[tuple(row[key] for key in dimensions)].append(row)
    rows = []
    for key, families in sorted(groups.items()):
        burdens = [(Decimal(row['burden']), row['weight']) for row in families if row['burden'] is not None]
        costs = [(row['net_operating_cents'], row['weight']) for row in families]
        weight = sum(row['weight'] for row in families)
        income_weight = sum(row['weight'] for row in families if row['period_income_cents'] is not None)
        aggregate_income = sum(row['period_income_cents'] * row['weight'] for row in families if row['period_income_cents'] is not None)
        matching_cost = sum(row['net_operating_cents'] * row['weight'] for row in families if row['period_income_cents'] is not None)
        rows.append({
            **dict(zip(dimensions, key)),
            'household_months': len(families),
            'distinct_households': len({row['household'] for row in families}),
            'weight': weight,
            'income_known_weight': income_weight,
            'income_missing_weight': weight - income_weight,
            'income_assumed_weight': sum(row['weight'] for row in families if row['income_assumed']),
            'positive_cost_zero_income_weight': sum(row['weight'] for row in families if row['positive_cost_zero_income']),
            'weighted_net_cost_cents': sum(row['net_operating_cents'] * row['weight'] for row in families),
            'weighted_subsidy_cents': sum(row['subsidy_cents'] * row['weight'] for row in families),
            'ratio_of_totals': display(Decimal(matching_cost) / aggregate_income) if aggregate_income else None,
            'burden_summary': summary(burdens),
            'cost_summary': summary(costs),
            'burden_above_threshold_share': display(threshold_share(burdens, threshold)),
            'burden_threshold': str(threshold),
            'trimmed_mean_burden': display(trimmed_mean(burdens, '0.1')),
            'net_cost_gini': display(gini(costs)),
        })
    return {'result_digest': result['result_digest'], 'dimensions': dimensions, 'population': population, 'rows': rows}


def income_missingness(result):
    verify(result)
    by_region = defaultdict(list)
    for row in result['families']:
        by_region[row['region']].append(row)
    rows = []
    for region, families in sorted(by_region.items()):
        total = sum(row['weight'] for row in families)
        missing = sum(row['weight'] for row in families if row['income_missing'])
        rows.append({
            'region': region,
            'sample_household_months': len(families),
            'distinct_households': len({row['household'] for row in families}),
            'weight': total,
            'missing_weight': missing,
            'missing_share': display(Decimal(missing) / total) if total else None,
            'assumed_weight': sum(row['weight'] for row in families if row['income_assumed']),
        })
    return rows
