from collections import defaultdict
from decimal import Decimal
from tariffs.primitives import digest
from tariffs.calendar import calendar_audit, capacity_calendar
from analysis.engine import verify, evaluate


def audit_result(catalog, result, replay=False):
    verify(result)
    if result['input_digest'] != catalog.fingerprint:
        raise ValueError('result belongs to another input')
    errors = []
    for charge in result['charges']['charges']:
        if sum(charge['daily_cents'].values()) != charge['gross_cents']:
            errors.append({'code': 'charge_day_allocation_mismatch', 'enrollment': charge['enrollment'], 'fee': charge['fee']})
    child_totals = {(row['child'], row['month']): row for row in result['children']}
    granted = defaultdict(int)
    family_caps = defaultdict(int)
    for row in result['subsidies']['allocations']:
        key = row['child'], row['month']
        granted[key] += row['granted_cents']
        family_caps[(row['household'], row['month'], row['program'])] += row['granted_cents']
        if row['granted_cents'] > row['requested_cents'] or row['granted_cents'] < 0:
            errors.append({'code': 'subsidy_outside_request', 'child': row['child'], 'program': row['program']})
    for key, value in family_caps.items():
        if value > catalog.subsidies[key[2]]['family_month_cap']:
            errors.append({'code': 'family_cap_exceeded', 'household': key[0], 'month': key[1], 'program': key[2]})
    for key, child in child_totals.items():
        if granted[key] > child['operating_cents']:
            errors.append({'code': 'child_cost_cap_exceeded', 'child': key[0], 'month': key[1]})
        if sum(child['daily_operating'].values()) != child['operating_cents']:
            errors.append({'code': 'child_daily_cost_mismatch', 'child': key[0], 'month': key[1]})
    family_keys = set()
    for row in result['families']:
        key = row['household'], row['month']
        if key in family_keys:
            errors.append({'code': 'duplicate_family_month', 'household': key[0], 'month': key[1]})
        family_keys.add(key)
        if row['net_operating_cents'] + row['deposit_cents'] != row['cash_outlay_cents']:
            errors.append({'code': 'family_cash_mismatch', 'household': key[0], 'month': key[1]})
        income = row['period_income_cents']
        if income:
            expected = format(Decimal(row['net_operating_cents']) / income, '.12f')
            if expected != row['burden']:
                errors.append({'code': 'family_burden_mismatch', 'household': key[0], 'month': key[1]})
        elif row['burden'] is not None:
            errors.append({'code': 'invalid_zero_or_missing_income_ratio', 'household': key[0], 'month': key[1]})
    for metric in ('operating_cents', 'deposit_cents', 'subsidy_cents', 'net_operating_cents', 'cash_outlay_cents'):
        if sum(row[metric] for row in result['families']) != result['totals'][metric]:
            errors.append({'code': 'total_mismatch', 'metric': metric})
    if replay:
        expected = evaluate(catalog, result['start'], result['end'], result['income_endpoint'], result['missing_income'], result['assumed_income_cents'])
        if expected != result:
            errors.append({'code': 'full_replay_mismatch'})
    capacity = capacity_calendar(catalog, result['start'], result['end'])
    return {
        'result_digest': result['result_digest'],
        'valid': not errors,
        'errors': errors,
        'calendar': calendar_audit(catalog, result['start'], result['end']),
        'provider_over_capacity_days': sum(row['enrollment_excess'] > 0 or row['attendance_excess'] > 0 for row in capacity),
        'full_replay_performed': replay,
    }


def input_quality(catalog, start, end):
    findings = []
    for ident, row in catalog.households.by_id.items():
        if row['income_low'] is None:
            findings.append({'code': 'income_missing', 'household': ident, 'severity': 'warning'})
        elif row['income_low'] != row['income_high']:
            findings.append({'code': 'income_interval', 'household': ident, 'severity': 'info'})
        if not row['children']:
            findings.append({'code': 'no_children', 'household': ident, 'severity': 'info'})
    calendar = calendar_audit(catalog, start, end)
    for row in calendar:
        if row['attendance_on_closed_days']:
            findings.append({'code': 'attendance_on_closed_day', 'enrollment': row['enrollment'], 'severity': 'error'})
    return {
        'input_digest': catalog.fingerprint,
        'findings': findings,
        'counts': {level: sum(row['severity'] == level for row in findings) for level in ('error', 'warning', 'info')},
        'calendar': calendar,
    }
