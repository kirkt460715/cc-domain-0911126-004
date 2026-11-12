from collections import defaultdict
from decimal import Decimal
from analysis.engine import verify
from analysis.providers import provider_attribution
from analysis.weighted import display, summary
from tariffs.calendar import enrollment_days, is_open
from tariffs.primitives import days


def cross_region_flows(catalog, result):
    verify(result)
    attributed = provider_attribution(catalog, result)
    groups = defaultdict(list)
    for row in attributed:
        groups[(row['household_region'], row['provider_region'], row['month'])].append(row)
    rows = []
    for key, values in sorted(groups.items()):
        rows.append({
            'household_region': key[0],
            'provider_region': key[1],
            'month': key[2],
            'cross_region': key[0] != key[1],
            'children': len({row['child'] for row in values}),
            'households': len({row['household'] for row in values}),
            'operating_cents': sum(row['gross_cents'] for row in values),
            'net_cents': sum(row['net_cents'] for row in values),
            'attributed_subsidy_cents': sum(row['attributed_subsidy_cents'] for row in values),
        })
    return {'result_digest': result['result_digest'], 'rows': rows, 'unit': 'observed_child_provider_month_not_population_migration'}


def attendance_costs(catalog, result):
    verify(result)
    attributed = provider_attribution(catalog, result)
    rows = []
    for cost in attributed:
        month = cost['month']
        relevant = [enrollment for enrollment in catalog.enrollments.values() if enrollment['child'] == cost['child'] and enrollment['provider'] == cost['provider']]
        service_days = set()
        attendance = set()
        for enrollment in relevant:
            provider = catalog.providers[enrollment['provider']]
            for day in enrollment_days(enrollment, result['start'], result['end']):
                if day[:7] != month:
                    continue
                if is_open(provider, day):
                    service_days.add(day)
                if day in enrollment['attendance']:
                    attendance.add(day)
        net = cost['net_cents']
        rows.append({
            'child': cost['child'],
            'provider': cost['provider'],
            'month': month,
            'service_days': len(service_days),
            'attendance_days': len(attendance),
            'net_cents': net,
            'net_per_service_day_cents': display(Decimal(net) / len(service_days)) if service_days else None,
            'net_per_attended_day_cents': display(Decimal(net) / len(attendance)) if attendance else None,
            'attendance_share': display(Decimal(len(attendance)) / len(service_days)) if service_days else None,
            'positive_cost_without_attendance': net > 0 and not attendance,
            'attribution': 'descriptive_charge_distribution_not_marginal_daily_price',
        })
    return rows


def enrollment_movements(catalog, start, end):
    days(start, end)
    groups = defaultdict(list)
    for enrollment in catalog.enrollments.values():
        groups[enrollment['child']].append(enrollment)
    rows = []
    for child, contracts in sorted(groups.items()):
        ordered = sorted(contracts, key=lambda row: row['start'])
        for left, right in zip(ordered, ordered[1:]):
            boundary = right['start']
            if not start <= boundary < end:
                continue
            rows.append({
                'child': child,
                'previous_provider': left['provider'],
                'next_provider': right['provider'],
                'previous_end': left['end'],
                'next_start': boundary,
                'continuous_contract': left['end'] == boundary,
                'provider_changed': left['provider'] != right['provider'],
                'previous_enrollment': left['id'],
                'next_enrollment': right['id'],
            })
    return rows


def cost_by_age(result):
    verify(result)
    groups = defaultdict(list)
    grants = defaultdict(int)
    for row in result['subsidies']['allocations']:
        grants[(row['child'], row['month'])] += row['granted_cents']
    weights = {(row['household'], row['month']): row['weight'] for row in result['families']}
    for row in result['children']:
        groups[(row['age_band'], row['month'])].append(row)
    output = []
    for (band, month), rows in sorted(groups.items()):
        net = [(row['operating_cents'] - grants[(row['child'], month)], weights[(row['household'], month)]) for row in rows]
        output.append({
            'age_band': band,
            'month': month,
            'child_months': len(rows),
            'distinct_households': len({row['household'] for row in rows}),
            'net_cost_summary': summary(net),
            'age_reference': 'first_report_day_in_month_not_average_attendance_age',
        })
    return output
