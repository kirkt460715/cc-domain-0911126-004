from collections import defaultdict
from decimal import Decimal
from analysis.engine import verify
from analysis.variants import paired_families
from analysis.weighted import display, summary


def compare(before, after):
    verify(before)
    verify(after)
    paired = paired_families(before, after)
    a = {(r['household'], r['month']): r for r in before['families']}
    b = {(r['household'], r['month']): r for r in after['families']}
    groups = defaultdict(list)
    changed_weights = []
    changes = []
    for row in paired['rows']:
        key = row['household'], row['month']
        old, new = a[key], b[key]
        if row['weight_changed']:
            changed_weights.append(key)
            continue
        income_changed = row['income_changed']
        burden_delta = None
        if old['burden'] is not None and new['burden'] is not None:
            burden_delta = Decimal(new['burden']) - Decimal(old['burden'])
        item = {**row, 'weight': old['weight'], 'burden_change': display(burden_delta), 'baseline_income_band': old['income_band']}
        changes.append(item)
        groups[old['income_band']].append(item)
    summary_rows = []
    for band, values in sorted(groups.items()):
        weight = sum(row['weight'] for row in values)
        known_burdens = [(row['burden_change'], row['weight']) for row in values if row['burden_change'] is not None]
        summary_rows.append({
            'baseline_income_band': band,
            'paired_household_months': len(values),
            'weight': weight,
            'weighted_net_cost_change_cents': sum(row['net_change_cents'] * row['weight'] for row in values),
            'weighted_subsidy_change_cents': sum(row['subsidy_change_cents'] * row['weight'] for row in values),
            'reduced_cost_weight': sum(row['weight'] for row in values if row['net_change_cents'] < 0),
            'increased_cost_weight': sum(row['weight'] for row in values if row['net_change_cents'] > 0),
            'unchanged_cost_weight': sum(row['weight'] for row in values if row['net_change_cents'] == 0),
            'burden_change_summary': summary(known_burdens),
            'income_changed_weight': sum(row['weight'] for row in values if row['income_changed']),
        })
    return {
        'before_digest': before['result_digest'],
        'after_digest': after['result_digest'],
        'paired': changes,
        'by_baseline_income': summary_rows,
        'unpaired': paired['unpaired'],
        'excluded_weight_changed_cells': [{'household': key[0], 'month': key[1]} for key in changed_weights],
        'interpretation': 'paired_descriptive_scenario_change_not_a_causal_estimate',
    }


def endpoint_envelope(low, high):
    verify(low)
    verify(high)
    if low['input_digest'] != high['input_digest']:
        raise ValueError('income endpoint envelope requires same input')
    if low['income_endpoint'] != 'low' or high['income_endpoint'] != 'high':
        raise ValueError('envelope requires low then high endpoint results')
    pairs = paired_families(low, high)
    rows = []
    for row in pairs['rows']:
        burdens = [Decimal(value) for value in (row['before_burden'], row['after_burden']) if value is not None]
        rows.append({
            'household': row['household'],
            'month': row['month'],
            'minimum_endpoint_net_cents': min(row['before_net_cents'], row['after_net_cents']),
            'maximum_endpoint_net_cents': max(row['before_net_cents'], row['after_net_cents']),
            'minimum_endpoint_burden': display(min(burdens)) if burdens else None,
            'maximum_endpoint_burden': display(max(burdens)) if burdens else None,
            'subsidy_eligibility_changes': row['subsidy_change_cents'] != 0,
            'bounds_are_endpoint_scenarios_only': True,
        })
    return {'input_digest': low['input_digest'], 'rows': rows, 'not_a_probability_interval': True, 'interior_income_thresholds_may_have_more_extreme_burden': True}
