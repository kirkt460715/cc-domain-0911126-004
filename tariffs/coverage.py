from collections import defaultdict
from tariffs.primitives import days, age_months
from tariffs.calendar import enrollment_days, is_open, fee_eligible, capacity_calendar


def fee_coverage(catalog, start, end):
    rows = []
    for enrollment in sorted(catalog.enrollments.values(), key=lambda row: row['id']):
        child = catalog.households.children[enrollment['child']]
        provider = catalog.providers[enrollment['provider']]
        versions = [fee for (pid, ident), values in catalog.fees.items() if pid == provider['id'] for fee in values]
        missing = []
        optional = []
        charged = 0
        for day in enrollment_days(enrollment, start, end):
            applicable = [fee for fee in versions if fee_eligible(fee, enrollment, child, day)]
            required = [fee for fee in applicable if fee['required'] and not fee['refundable']]
            if required:
                charged += 1
            elif is_open(provider, day):
                missing.append(day)
            if any(not fee['required'] for fee in applicable):
                optional.append(day)
        rows.append({
            'enrollment': enrollment['id'],
            'provider': provider['id'],
            'required_fee_covered_calendar_days': charged,
            'open_days_without_required_operating_fee': missing,
            'optional_fee_days': optional,
            'interpretation': 'absence_of_required_tariff_is_review_signal_not_imputed_free_or_invalid_contract',
        })
    return rows


def occupancy_summary(catalog, start, end):
    daily = capacity_calendar(catalog, start, end)
    groups = defaultdict(list)
    for row in daily:
        groups[(row['provider'], row['day'][:7])].append(row)
    rows = []
    for (provider, month), values in sorted(groups.items()):
        service = [row for row in values if row['open']]
        available = sum(row['capacity'] for row in service)
        occupied = sum(row['enrolled'] for row in service)
        rows.append({
            'provider': provider,
            'month': month,
            'service_days': len(service),
            'place_days': available,
            'enrolled_place_days': occupied,
            'attended_place_days': sum(row['attendance'] for row in values),
            'enrollment_excess_place_days': sum(row['enrollment_excess'] for row in values),
            'attendance_excess_place_days': sum(row['attendance_excess'] for row in values),
            'peak_enrolled': max((row['enrolled'] for row in values), default=0),
            'utilization': None if not available else format(occupied / available, '.12f'),
        })
    return rows


def age_transitions(catalog, start, end):
    report_days = days(start, end)
    rows = []
    for child in sorted(catalog.households.children.values(), key=lambda row: row['id']):
        previous = age_months(child['birth'], report_days[0])
        for day in report_days[1:]:
            current = age_months(child['birth'], day)
            if current != previous:
                affected = []
                for (provider, fee_id), versions in catalog.fees.items():
                    if any(previous < fee['min_age'] <= current or previous < fee['max_age'] <= current for fee in versions):
                        affected.append({'provider': provider, 'fee': fee_id})
                programs = [program['id'] for program in catalog.subsidies.values() if previous < program['min_age'] <= current or previous < program['max_age'] <= current]
                rows.append({
                    'child': child['id'],
                    'day': day,
                    'previous_completed_months': previous,
                    'completed_months': current,
                    'potential_fee_boundaries': affected,
                    'potential_program_boundaries': sorted(programs),
                })
            previous = current
    return rows


def tariff_change_points(catalog, start, end):
    days(start, end)
    points = defaultdict(list)
    for (provider, ident), versions in catalog.fees.items():
        for fee in versions:
            for key in ('start', 'end'):
                day = fee[key]
                if start <= day < end:
                    points[day].append({'table': 'fees', 'provider': provider, 'id': ident, 'boundary': key, 'amount': fee['amount']})
    for ident, program in catalog.subsidies.items():
        for key in ('start', 'end'):
            day = program[key]
            if start <= day < end:
                points[day].append({'table': 'subsidies', 'id': ident, 'boundary': key, 'value': program['value']})
    return [{'day': day, 'changes': sorted(values, key=lambda row: (row['table'], row['id'], row['boundary']))} for day, values in sorted(points.items())]
