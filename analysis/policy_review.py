from collections import defaultdict
from tariffs.primitives import days, digest
from analysis.engine import evaluate, verify
from analysis.variants import change, paired_families
from analysis.explanations import subsidy_waterfall


def program_overlap(catalog, start, end):
    active_days = days(start, end)
    programs = sorted(catalog.subsidies.values(), key=lambda row: row['id'])
    rows = []
    for index, left in enumerate(programs):
        for right in programs[index + 1:]:
            common = [day for day in active_days if left['start'] <= day < left['end'] and right['start'] <= day < right['end']]
            age_low = max(left['min_age'], right['min_age'])
            age_high = min(left['max_age'], right['max_age'])
            regions_match = left['region'] == '*' or right['region'] == '*' or left['region'] == right['region']
            if not common or age_low >= age_high or not regions_match:
                continue
            rows.append({
                'left': left['id'],
                'right': right['id'],
                'common_days': len(common),
                'age_min_months': age_low,
                'age_max_months_exclusive': age_high,
                'common_income_ceiling_cents': min(left['income_ceiling'], right['income_ceiling']),
                'same_stack_group': left['stack_group'] == right['stack_group'],
                'mechanism': 'choose_one_by_request_priority_id' if left['stack_group'] == right['stack_group'] else 'stack_with_child_gross_cap',
            })
    return rows


def remove_programs(catalog, program_ids):
    if not isinstance(program_ids, list) or len(program_ids) != len(set(program_ids)):
        raise ValueError('expected unique program ids')
    if set(program_ids) - catalog.subsidies.keys():
        raise ValueError('unknown program to remove')
    data = catalog.export()
    data['subsidies'] = [row for row in data['subsidies'] if row['id'] not in program_ids]
    from tariffs.catalog import Catalog
    return Catalog(data)


def leave_one_program_out(catalog, result):
    verify(result)
    if result['input_digest'] != catalog.fingerprint:
        raise ValueError('baseline input mismatch')
    rows = []
    for ident in sorted(catalog.subsidies):
        variant = remove_programs(catalog, [ident])
        other = evaluate(variant, result['start'], result['end'], result['income_endpoint'], result['missing_income'], result['assumed_income_cents'])
        differences = paired_families(result, other)
        rows.append({
            'removed_program': ident,
            'net_cost_increase_cents': other['totals']['net_operating_cents'] - result['totals']['net_operating_cents'],
            'subsidy_reduction_cents': result['totals']['subsidy_cents'] - other['totals']['subsidy_cents'],
            'household_months_changed': sum(row['net_change_cents'] != 0 for row in differences['rows']),
            'alternative_result_digest': other['result_digest'],
            'alternative_program_waterfall': subsidy_waterfall(other),
        })
    return {'baseline_result_digest': result['result_digest'], 'rows': rows, 'interpretation': 'mechanical_leave_one_out_not_additive_attribution_or_causal_effect'}


def budget_sweep(catalog, result, program, caps):
    verify(result)
    if program not in catalog.subsidies:
        raise KeyError(program)
    if not isinstance(caps, list) or len(caps) > 100 or len(caps) != len(set(caps)):
        raise ValueError('expected bounded unique cap list')
    rows = []
    for cap in sorted(caps):
        variant = change(catalog.document, [{'table': 'subsidies', 'id': program, 'field': 'family_month_cap', 'value': cap}])
        other = evaluate(variant, result['start'], result['end'], result['income_endpoint'], result['missing_income'], result['assumed_income_cents'])
        pairs = paired_families(result, other)['rows']
        rows.append({
            'family_month_cap_cents': cap,
            'total_subsidy_cents': other['totals']['subsidy_cents'],
            'total_net_cost_cents': other['totals']['net_operating_cents'],
            'net_change_cents': other['totals']['net_operating_cents'] - result['totals']['net_operating_cents'],
            'benefiting_household_months': sum(row['net_change_cents'] < 0 for row in pairs),
            'worsening_household_months': sum(row['net_change_cents'] > 0 for row in pairs),
            'result_digest': other['result_digest'],
        })
    return {'program': program, 'rows': rows, 'scope': 'per_family_cap_sweep_not_a_total_public_budget_optimizer'}


def program_recipients(result):
    verify(result)
    groups = defaultdict(list)
    for row in result['subsidies']['allocations']:
        groups[row['program']].append(row)
    output = []
    for program, values in sorted(groups.items()):
        selected = [row for row in values if row['requested_cents'] > 0]
        recipients = [row for row in values if row['granted_cents'] > 0]
        output.append({
            'program': program,
            'positive_selected_child_months': len(selected),
            'positive_granted_child_months': len(recipients),
            'selected_but_capped_to_zero': sum(row['requested_cents'] > 0 and row['granted_cents'] == 0 for row in values),
            'households': sorted({row['household'] for row in recipients}),
            'granted_cents': sum(row['granted_cents'] for row in recipients),
            'unfunded_selected_cents': sum(row['requested_cents'] - row['granted_cents'] for row in values),
        })
    return output
