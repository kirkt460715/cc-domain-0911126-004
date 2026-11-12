from collections import defaultdict
from decimal import Decimal
from tariffs.rounding import capped_proportional
from analysis.engine import verify
from analysis.weighted import summary, display


def provider_attribution(catalog, result):
    verify(result)
    amounts = defaultdict(int)
    deposits = defaultdict(int)
    for row in result['charges']['charges']:
        key = row['child'], row['month'], row['provider']
        if row['refundable']:
            deposits[key] += row['gross_cents']
        else:
            amounts[key] += row['gross_cents']
    grants = defaultdict(int)
    for row in result['subsidies']['allocations']:
        grants[(row['child'], row['month'])] += row['granted_cents']
    attributed = []
    for child in result['children']:
        child_id, month = child['child'], child['month']
        household = catalog.households.by_id[child['household']]
        requests = {provider: amounts[(child_id, month, provider)] for provider in child['providers']}
        allocation = capped_proportional(requests, grants[(child_id, month)])
        for provider in sorted(child['providers']):
            gross = amounts[(child_id, month, provider)]
            deposit = deposits[(child_id, month, provider)]
            subsidy = allocation[provider]
            attributed.append({
                'provider': provider,
                'provider_region': catalog.providers[provider]['region'],
                'household': household['id'],
                'household_region': household['region'],
                'child': child_id,
                'month': month,
                'weight': household['weight'],
                'gross_cents': gross,
                'deposit_cents': deposit,
                'attributed_subsidy_cents': subsidy,
                'net_cents': gross - subsidy,
                'attribution': 'proportional_operating_charges_not_a_payment_instruction',
            })
    if sum(row['attributed_subsidy_cents'] for row in attributed) != result['totals']['subsidy_cents']:
        raise AssertionError('provider subsidy attribution does not conserve')
    if sum(row['net_cents'] for row in attributed) != result['totals']['net_operating_cents']:
        raise AssertionError('provider net attribution does not conserve')
    return attributed


def provider_report(catalog, result):
    rows = provider_attribution(catalog, result)
    groups = defaultdict(list)
    for row in rows:
        groups[(row['provider'], row['month'])].append(row)
    reports = []
    for key, values in sorted(groups.items()):
        reports.append({
            'provider': key[0],
            'month': key[1],
            'distinct_children': len({row['child'] for row in values}),
            'distinct_households': len({row['household'] for row in values}),
            'observed_gross_cents': sum(row['gross_cents'] for row in values),
            'observed_subsidy_cents': sum(row['attributed_subsidy_cents'] for row in values),
            'weighted_gross_cents': sum(row['gross_cents'] * row['weight'] for row in values),
            'weighted_net_cents': sum(row['net_cents'] * row['weight'] for row in values),
            'net_cost_summary': summary([(row['net_cents'], row['weight']) for row in values]),
            'cross_region_children': len({row['child'] for row in values if row['household_region'] != row['provider_region']}),
        })
    return {'result_digest': result['result_digest'], 'rows': reports, 'attributions': rows}


def subsidy_incidence(catalog, result):
    groups = defaultdict(lambda: {'gross': 0, 'subsidy': 0, 'weight': 0, 'families': set()})
    for family in result['families']:
        key = family['income_band']
        group = groups[key]
        group['gross'] += family['operating_cents'] * family['weight']
        group['subsidy'] += family['subsidy_cents'] * family['weight']
        group['weight'] += family['weight']
        group['families'].add(family['household'])
    total_grants = sum(row['subsidy'] for row in groups.values())
    rows = []
    for band, group in sorted(groups.items()):
        rows.append({
            'income_band': band,
            'weighted_gross_cents': group['gross'],
            'weighted_subsidy_cents': group['subsidy'],
            'household_month_weight': group['weight'],
            'distinct_households': len(group['families']),
            'subsidy_share': display(Decimal(group['subsidy']) / total_grants) if total_grants else None,
            'gross_cost_offset': display(Decimal(group['subsidy']) / group['gross']) if group['gross'] else None,
        })
    return {'result_digest': result['result_digest'], 'rows': rows, 'interpretation': 'weighted_sample_incidence_not_population_fiscal_total'}
