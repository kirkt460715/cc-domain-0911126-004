from collections import defaultdict
from decimal import Decimal
from tariffs.primitives import days, month_start, month_end, months, digest
from tariffs.calendar import is_open, month_service_days, enrollment_days, fee_eligible, first_once_day, require_valid_attendance
from tariffs.rounding import allocate_exact


def fee_days(catalog, enrollment, fee_id, versions, start, end):
    provider = catalog.providers[enrollment['provider']]
    child = catalog.households.children[enrollment['child']]
    once = first_once_day(versions, enrollment, child) if versions[0]['kind'] == 'once' else None
    result = defaultdict(dict)
    for day in enrollment_days(enrollment, start, end):
        applicable = [fee for fee in versions if fee_eligible(fee, enrollment, child, day)]
        if not applicable:
            continue
        fee = applicable[0]
        amount = Decimal(fee['amount'])
        kind = fee['kind']
        if kind == 'monthly':
            if fee['proration'] == 'service':
                eligible_days = month_service_days(provider, day)
                if day not in eligible_days:
                    continue
            else:
                eligible_days = days(month_start(day), month_end(day))
            amount /= len(eligible_days)
        elif kind == 'daily':
            if not is_open(provider, day):
                continue
        elif kind == 'attendance':
            if day not in enrollment['attendance']:
                continue
        elif kind == 'once':
            if day != once:
                continue
        key = day[:7], fee['refundable']
        result[key][day] = amount
    return result


def calculate(catalog, start, end):
    days(start, end)
    require_valid_attendance(catalog)
    rows = []
    for enrollment_id, enrollment in sorted(catalog.enrollments.items()):
        household = catalog.households.children[enrollment['child']]['household']
        for (provider_id, fee_id), versions in sorted(catalog.fees.items()):
            if provider_id != enrollment['provider']:
                continue
            portions = fee_days(catalog, enrollment, fee_id, versions, start, end)
            for (month, refundable), values in sorted(portions.items()):
                allocated = allocate_exact(values)
                row = {
                    'enrollment': enrollment_id,
                    'household': household,
                    'child': enrollment['child'],
                    'provider': provider_id,
                    'fee': fee_id,
                    'month': month,
                    'refundable': refundable,
                    'gross_cents': sum(allocated.values()),
                    'daily_cents': allocated,
                    'rounding': 'half_up_per_logical_fee_enrollment_month_then_largest_remainder_days',
                }
                rows.append(row)
    return {'start': start, 'end': end, 'input_digest': catalog.fingerprint, 'charges': rows, 'gross_cents': sum(r['gross_cents'] for r in rows)}


def child_months(catalog, charges, start, end):
    result = {}
    for enrollment in catalog.enrollments.values():
        household_id = catalog.households.children[enrollment['child']]['household']
        for day in enrollment_days(enrollment, start, end):
            key = enrollment['child'], day[:7]
            row = result.setdefault(key, {
                'child': enrollment['child'],
                'household': household_id,
                'month': day[:7],
                'operating_cents': 0,
                'deposit_cents': 0,
                'daily_operating': {},
                'enrolled_days': [],
                'providers': [],
                'enrollments': [],
            })
            row['enrolled_days'].append(day)
            if enrollment['provider'] not in row['providers']:
                row['providers'].append(enrollment['provider'])
            if enrollment['id'] not in row['enrollments']:
                row['enrollments'].append(enrollment['id'])
    for charge in charges['charges']:
        row = result[(charge['child'], charge['month'])]
        metric = 'deposit_cents' if charge['refundable'] else 'operating_cents'
        row[metric] += charge['gross_cents']
        if not charge['refundable']:
            for day, amount in charge['daily_cents'].items():
                row['daily_operating'][day] = row['daily_operating'].get(day, 0) + amount
    return [result[key] for key in sorted(result)]
