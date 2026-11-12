from collections import defaultdict
from tariffs.primitives import fields, integer, word, digest
from analysis.engine import verify
from analysis.providers import provider_attribution


CLAIM_METRICS = ('gross_cents', 'deposit_cents', 'attributed_subsidy_cents', 'net_cents')


def validate_claims(claims):
    if not isinstance(claims, list) or len(claims) > 100000:
        raise ValueError('claims must be bounded list')
    seen = set()
    rows = []
    for claim in claims:
        fields(claim, ('provider', 'child', 'month', *CLAIM_METRICS))
        word(claim['provider'], 'claim.provider')
        word(claim['child'], 'claim.child')
        month = claim['month']
        if not isinstance(month, str) or len(month) != 7:
            raise ValueError('claim month must be YYYY-MM')
        from tariffs.primitives import iso
        iso(month + '-01', 'claim.month')
        for metric in CLAIM_METRICS:
            integer(claim[metric], 'claim.' + metric)
        if claim['gross_cents'] - claim['attributed_subsidy_cents'] != claim['net_cents']:
            raise ValueError('claim net amount does not reconcile')
        key = claim['provider'], claim['child'], month
        if key in seen:
            raise ValueError('duplicate provider child month claim')
        seen.add(key)
        rows.append(dict(claim))
    return rows


def reconcile(catalog, result, claims, tolerance_cents=0):
    verify(result)
    integer(tolerance_cents, 'tolerance_cents', 0, 1000000)
    declared = validate_claims(claims)
    expected = provider_attribution(catalog, result)
    old = {(row['provider'], row['child'], row['month']): row for row in expected}
    new = {(row['provider'], row['child'], row['month']): row for row in declared}
    rows = []
    for key in sorted(old.keys() | new.keys()):
        actual = new.get(key)
        reference = old.get(key)
        differences = {}
        if actual is not None and reference is not None:
            differences = {metric: actual[metric] - reference[metric] for metric in CLAIM_METRICS}
            status = 'matched' if all(abs(value) <= tolerance_cents for value in differences.values()) else 'amount_mismatch'
        elif actual is None:
            status = 'missing_claim'
        else:
            status = 'unexpected_claim'
        rows.append({
            'provider': key[0],
            'child': key[1],
            'month': key[2],
            'status': status,
            'expected': None if reference is None else {metric: reference[metric] for metric in CLAIM_METRICS},
            'claimed': None if actual is None else {metric: actual[metric] for metric in CLAIM_METRICS},
            'differences_cents': differences,
        })
    body = {
        'result_digest': result['result_digest'],
        'claim_digest': digest(declared),
        'tolerance_cents': tolerance_cents,
        'rows': rows,
        'matched': all(row['status'] == 'matched' for row in rows),
        'counts': {status: sum(row['status'] == status for row in rows) for status in ('matched', 'missing_claim', 'unexpected_claim', 'amount_mismatch')},
        'scope': 'comparison_to_analytical_provider_attribution_not_payment_approval',
    }
    return {**body, 'reconciliation_digest': digest(body)}


def provider_differences(report):
    check = digest({key: value for key, value in report.items() if key != 'reconciliation_digest'})
    if check != report.get('reconciliation_digest'):
        raise ValueError('reconciliation digest mismatch')
    groups = defaultdict(list)
    for row in report['rows']:
        groups[row['provider']].append(row)
    output = []
    for provider, rows in sorted(groups.items()):
        output.append({
            'provider': provider,
            'matched_child_months': sum(row['status'] == 'matched' for row in rows),
            'exception_child_months': sum(row['status'] != 'matched' for row in rows),
            'expected_cents': {metric: sum((row['expected'] or {}).get(metric, 0) for row in rows) for metric in CLAIM_METRICS},
            'claimed_cents': {metric: sum((row['claimed'] or {}).get(metric, 0) for row in rows) for metric in CLAIM_METRICS},
            'missing_claim_children': sorted({row['child'] for row in rows if row['status'] == 'missing_claim'}),
            'unexpected_claim_children': sorted({row['child'] for row in rows if row['status'] == 'unexpected_claim'}),
        })
    return output
