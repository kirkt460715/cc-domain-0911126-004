from collections import defaultdict
from decimal import Decimal
from tariffs.primitives import days, months, month_start, month_end, cents, digest, integer
from tariffs.charges import calculate, child_months
from tariffs.subsidies import apply
from households.bands import income_band, child_age_band, income_interval_bands


def income_months(catalog, start, end, endpoint, missing, assumed_income):
    incomes = {}
    for ident in sorted(catalog.households.by_id):
        incomes[ident] = catalog.households.income(ident, endpoint, missing, assumed_income)
    return incomes


def family_rows(catalog, child_rows, subsidies, incomes, start, end):
    grants = defaultdict(int)
    for allocation in subsidies['allocations']:
        grants[(allocation['child'], allocation['month'])] += allocation['granted_cents']
    rows = []
    by_family = defaultdict(list)
    for row in child_rows:
        by_family[(row['household'], row['month'])].append(row)
    for ident, household in sorted(catalog.households.by_id.items()):
        income = incomes[ident]
        annual = income['annual_cents']
        for month in months(start, end):
            children = by_family[(ident, month)]
            first = max(start, month + '-01')
            last = min(end, month_end(month + '-01'))
            exposed = len(days(first, last))
            full = len(days(month + '-01', month_end(month + '-01')))
            period_income = cents(Decimal(annual) / 12 * exposed / full) if annual is not None else None
            operating = sum(row['operating_cents'] for row in children)
            deposit = sum(row['deposit_cents'] for row in children)
            subsidy = sum(grants[(row['child'], month)] for row in children)
            net = operating - subsidy
            if net < 0:
                raise AssertionError('net household cost cannot be negative')
            burden = Decimal(net) / period_income if period_income else None
            rows.append({
                'household': ident,
                'month': month,
                'region': household['region'],
                'income_band': income_band(annual),
                'interval_band': income_interval_bands(household),
                'weight': household['weight'],
                'annual_income_cents': annual,
                'period_income_cents': period_income,
                'income_assumed': income['assumed'],
                'income_source': income['source'],
                'exposure_days': exposed,
                'full_month_days': full,
                'children_enrolled': len(children),
                'operating_cents': operating,
                'deposit_cents': deposit,
                'subsidy_cents': subsidy,
                'net_operating_cents': net,
                'cash_outlay_cents': net + deposit,
                'burden': None if burden is None else format(burden, '.12f'),
                'positive_cost_zero_income': period_income == 0 and net > 0,
                'income_missing': period_income is None,
            })
    return rows


def evaluate(catalog, start, end, endpoint='low', missing='exclude', assumed_income=None):
    days(start, end)
    if endpoint not in ('low', 'high') or missing not in ('exclude', 'assume'):
        raise ValueError('invalid income policy')
    if missing == 'assume':
        integer(assumed_income, 'assumed_income')
    elif assumed_income is not None:
        raise ValueError('assumed income only permitted with assume mode')
    incomes = income_months(catalog, start, end, endpoint, missing, assumed_income)
    charges = calculate(catalog, start, end)
    children = child_months(catalog, charges, start, end)
    subsidies = apply(catalog, children, incomes)
    families = family_rows(catalog, children, subsidies, incomes, start, end)
    for child in children:
        child['age_band'] = child_age_band(catalog.households.children[child['child']], max(start, child['month'] + '-01'))
    body = {
        'version': 1,
        'input_digest': catalog.fingerprint,
        'start': start,
        'end': end,
        'income_endpoint': endpoint,
        'missing_income': missing,
        'assumed_income_cents': assumed_income,
        'charges': charges,
        'children': children,
        'subsidies': subsidies,
        'families': families,
        'totals': {
            'operating_cents': sum(row['operating_cents'] for row in families),
            'deposit_cents': sum(row['deposit_cents'] for row in families),
            'subsidy_cents': sum(row['subsidy_cents'] for row in families),
            'net_operating_cents': sum(row['net_operating_cents'] for row in families),
            'cash_outlay_cents': sum(row['cash_outlay_cents'] for row in families),
        },
    }
    return {**body, 'result_digest': digest(body)}


def verify(result):
    expected = digest({key: value for key, value in result.items() if key != 'result_digest'})
    if result.get('result_digest') != expected:
        raise ValueError('affordability result digest mismatch')
    totals = result['totals']
    if totals['operating_cents'] - totals['subsidy_cents'] != totals['net_operating_cents']:
        raise ValueError('total net cost does not conserve')
    if totals['net_operating_cents'] + totals['deposit_cents'] != totals['cash_outlay_cents']:
        raise ValueError('total cash cost does not conserve')
    for row in result['families']:
        if row['operating_cents'] - row['subsidy_cents'] != row['net_operating_cents']:
            raise ValueError('family net cost does not conserve')
        if row['net_operating_cents'] < 0:
            raise ValueError('negative family cost')
    return True
