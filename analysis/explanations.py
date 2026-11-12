from collections import defaultdict
from decimal import Decimal
from tariffs.primitives import digest
from analysis.engine import verify
from analysis.weighted import display


def family_statement(catalog, result, household):
    verify(result)
    if result['input_digest'] != catalog.fingerprint:
        raise ValueError('statement input mismatch')
    if household not in catalog.households.by_id:
        raise KeyError(household)
    registry = catalog.households.by_id[household]
    months = []
    for family in result['families']:
        if family['household'] != household:
            continue
        month = family['month']
        children = []
        for child in result['children']:
            if child['household'] != household or child['month'] != month:
                continue
            charges = [row for row in result['charges']['charges'] if row['child'] == child['child'] and row['month'] == month]
            grants = [row for row in result['subsidies']['allocations'] if row['child'] == child['child'] and row['month'] == month]
            trace = [row for row in result['subsidies']['eligibility_trace'] if row['child'] == child['child'] and row['month'] == month]
            net = child['operating_cents'] - sum(row['granted_cents'] for row in grants)
            children.append({
                'child': child['child'],
                'age_band': child['age_band'],
                'providers': child['providers'],
                'enrollment_days': len(child['enrolled_days']),
                'charges': charges,
                'subsidy_allocations': grants,
                'eligibility_trace': trace,
                'operating_cents': child['operating_cents'],
                'deposit_cents': child['deposit_cents'],
                'net_operating_cents': net,
                'cash_outlay_cents': net + child['deposit_cents'],
            })
        if sum(row['net_operating_cents'] for row in children) != family['net_operating_cents']:
            raise ValueError('statement does not reconcile')
        months.append({'summary': family, 'children': children})
    body = {
        'input_digest': catalog.fingerprint,
        'result_digest': result['result_digest'],
        'household': household,
        'income_interval': [registry['income_low'], registry['income_high']],
        'sample_weight': registry['weight'],
        'months': months,
        'purpose': 'analytical_cost_explanation_not_invoice_or_payment_instruction',
    }
    return {**body, 'statement_digest': digest(body)}


def fee_contributions(result):
    verify(result)
    groups = defaultdict(list)
    for row in result['charges']['charges']:
        groups[(row['provider'], row['fee'], row['refundable'])].append(row)
    output = []
    total = result['totals']['operating_cents']
    for key, charges in sorted(groups.items()):
        amount = sum(row['gross_cents'] for row in charges)
        share = Decimal(amount) / total if total and not key[2] else None
        output.append({
            'provider': key[0],
            'fee': key[1],
            'refundable': key[2],
            'gross_cents': amount,
            'children': len({row['child'] for row in charges}),
            'households': len({row['household'] for row in charges}),
            'charged_months': len({row['month'] for row in charges}),
            'operating_share': display(share),
        })
    return output


def subsidy_waterfall(result):
    verify(result)
    groups = defaultdict(list)
    for row in result['subsidies']['allocations']:
        groups[(row['program'], row['month'])].append(row)
    output = []
    for key, values in sorted(groups.items()):
        requested = sum(row['requested_cents'] for row in values)
        capped = sum(row['after_family_cap_cents'] for row in values)
        granted = sum(row['granted_cents'] for row in values)
        output.append({
            'program': key[0],
            'month': key[1],
            'selected_requests_cents': requested,
            'after_family_cap_cents': capped,
            'family_cap_reduction_cents': requested - capped,
            'after_child_cap_cents': granted,
            'child_cap_reduction_cents': capped - granted,
            'recipient_children': sum(row['granted_cents'] > 0 for row in values),
            'recipient_households': len({row['household'] for row in values if row['granted_cents'] > 0}),
        })
    return output


def eligibility_reasons(result):
    verify(result)
    totals = defaultdict(lambda: defaultdict(int))
    for row in result['subsidies']['eligibility_trace']:
        for reason, count in row['reasons'].items():
            totals[row['program']][reason] += count
    return [{'program': program, 'child_days_by_reason': dict(sorted(reasons.items()))} for program, reasons in sorted(totals.items())]
