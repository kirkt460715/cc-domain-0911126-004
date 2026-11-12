from collections import defaultdict
from decimal import Decimal
from tariffs.primitives import age_months, days, month_start, month_end, cents
from tariffs.rounding import capped_proportional


def eligibility(program, household, child, day, annual_income):
    if annual_income is None:
        return False, 'income_unknown'
    if not program['start'] <= day < program['end']:
        return False, 'outside_program_validity'
    if program['region'] != '*' and program['region'] != household['region']:
        return False, 'region_not_eligible'
    age = age_months(child['birth'], day)
    if not program['min_age'] <= age < program['max_age']:
        return False, 'age_not_eligible'
    if annual_income > program['income_ceiling']:
        return False, 'income_above_ceiling'
    return True, 'eligible'


def requests(catalog, child_rows, incomes):
    results = []
    for child_row in child_rows:
        child = catalog.households.children[child_row['child']]
        household = catalog.households.by_id[child_row['household']]
        income = incomes[household['id']]['annual_cents']
        for program_id, program in sorted(catalog.subsidies.items()):
            eligible = []
            reasons = defaultdict(int)
            for day in child_row['enrolled_days']:
                allowed, reason = eligibility(program, household, child, day, income)
                reasons[reason] += 1
                if allowed:
                    eligible.append(day)
            if program['mode'] == 'flat':
                full_month = days(month_start(child_row['month'] + '-01'), month_end(child_row['month'] + '-01'))
                amount = cents(Decimal(program['value']) * len(eligible) / len(full_month))
            else:
                base = sum(child_row['daily_operating'].get(day, 0) for day in eligible)
                amount = cents(Decimal(base) * program['value'] / 10000)
            amount = min(amount, child_row['operating_cents'])
            results.append({
                'household': household['id'],
                'child': child['id'],
                'month': child_row['month'],
                'program': program_id,
                'stack_group': program['stack_group'],
                'priority': program['priority'],
                'requested_cents': amount,
                'eligible_days': len(eligible),
                'reasons': dict(sorted(reasons.items())),
            })
    return results


def apply(catalog, child_rows, incomes):
    candidates = requests(catalog, child_rows, incomes)
    grouped = defaultdict(list)
    for candidate in candidates:
        grouped[(candidate['child'], candidate['month'], candidate['stack_group'])].append(candidate)
    chosen = []
    trace = []
    for key, values in sorted(grouped.items()):
        best = min(values, key=lambda row: (-row['requested_cents'], -row['priority'], row['program']))
        chosen.append(dict(best))
        for candidate in values:
            trace.append({**candidate, 'selected_in_stack_group': candidate is best})
    by_program_family = defaultdict(list)
    for row in chosen:
        by_program_family[(row['household'], row['month'], row['program'])].append(row)
    for key, values in by_program_family.items():
        program = catalog.subsidies[key[2]]
        allocation = capped_proportional({row['child']: row['requested_cents'] for row in values}, program['family_month_cap'])
        for row in values:
            row['after_family_cap_cents'] = allocation[row['child']]
    remaining = {(row['child'], row['month']): row['operating_cents'] for row in child_rows}
    allocations = []
    for row in sorted(chosen, key=lambda row: (-row['priority'], row['program'], row['child'], row['month'])):
        key = row['child'], row['month']
        amount = min(row['after_family_cap_cents'], remaining[key])
        remaining[key] -= amount
        allocations.append({**row, 'granted_cents': amount, 'gross_cap_reduction_cents': row['after_family_cap_cents'] - amount})
    return {'allocations': allocations, 'eligibility_trace': trace, 'total_granted_cents': sum(row['granted_cents'] for row in allocations), 'remaining_child_month_cents': [{'child': key[0], 'month': key[1], 'net_cents': value} for key, value in sorted(remaining.items())]}
