from decimal import Decimal
from tariffs.catalog import Catalog
from tariffs.primitives import integer
from analysis.engine import evaluate
from analysis.weighted import display


def critical_incomes(catalog, household_id):
    household = catalog.households.by_id[household_id]
    low, high = household['income_low'], household['income_high']
    if low is None:
        raise ValueError('critical income scan requires observed bounds')
    values = {low, high}
    for program in catalog.subsidies.values():
        if program['region'] not in ('*', household['region']):
            continue
        ceiling = program['income_ceiling']
        for value in (ceiling, ceiling + 1):
            if low <= value <= high:
                values.add(value)
    return sorted(values)


def household_envelope(catalog, household_id, start, end):
    grid = critical_incomes(catalog, household_id)
    cases = []
    for income in grid:
        document = catalog.export()
        for row in document['households']:
            if row['id'] == household_id:
                row['income_low'] = income
                row['income_high'] = income
        result = evaluate(Catalog(document), start, end)
        families = [row for row in result['families'] if row['household'] == household_id]
        cases.append({'annual_income_cents': income, 'months': families})
    months = sorted({row['month'] for case in cases for row in case['months']})
    envelopes = []
    for month in months:
        rows = [row for case in cases for row in case['months'] if row['month'] == month]
        burdens = [(Decimal(row['burden']), row['annual_income_cents']) for row in rows if row['burden'] is not None]
        minimum = min(burdens) if burdens else None
        maximum = max(burdens) if burdens else None
        envelopes.append({
            'month': month,
            'minimum_burden': display(minimum[0]) if minimum else None,
            'minimum_at_income_cents': minimum[1] if minimum else None,
            'maximum_burden': display(maximum[0]) if maximum else None,
            'maximum_at_income_cents': maximum[1] if maximum else None,
            'zero_income_positive_cost_present': any(row['positive_cost_zero_income'] for row in rows),
            'minimum_net_cents': min(row['net_operating_cents'] for row in rows),
            'maximum_net_cents': max(row['net_operating_cents'] for row in rows),
        })
    return {
        'household': household_id,
        'input_digest': catalog.fingerprint,
        'critical_income_cents': grid,
        'cases': cases,
        'envelopes': envelopes,
        'scope': 'fixed_household_weights_fee_rules_and_stepwise_income_eligibility',
        'integer_income_rounding_can_create_additional_local_ratio_steps': True,
    }
