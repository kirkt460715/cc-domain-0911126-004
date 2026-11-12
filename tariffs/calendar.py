from datetime import date
from tariffs.primitives import days, month_start, month_end, age_months, InvalidInput


def is_open(provider, day):
    if day in provider['closed']:
        return False
    if day in provider['extra_open']:
        return True
    return date.fromisoformat(day).weekday() in provider['weekdays']


def month_service_days(provider, day):
    return [value for value in days(month_start(day), month_end(day)) if is_open(provider, value)]


def enrollment_days(enrollment, start, end):
    first = max(enrollment['start'], start)
    last = min(enrollment['end'], end)
    return days(first, last) if first < last else []


def fee_eligible(fee, enrollment, child, day):
    if not enrollment['start'] <= day < enrollment['end']:
        return False
    if not fee['start'] <= day < fee['end']:
        return False
    if not fee['required'] and fee['id'] not in enrollment['options']:
        return False
    age = age_months(child['birth'], day)
    return fee['min_age'] <= age < fee['max_age']


def first_once_day(versions, enrollment, child):
    for day in days(enrollment['start'], enrollment['end']):
        if any(fee_eligible(fee, enrollment, child, day) for fee in versions):
            return day
    return None


def calendar_audit(catalog, start, end):
    result = []
    for enrollment in catalog.enrollments.values():
        provider = catalog.providers[enrollment['provider']]
        selected = enrollment_days(enrollment, start, end)
        invalid = [day for day in enrollment['attendance'] if day in selected and not is_open(provider, day)]
        result.append({
            'enrollment': enrollment['id'],
            'provider': provider['id'],
            'calendar_days': len(selected),
            'service_days': sum(is_open(provider, day) for day in selected),
            'attendance_days': sum(day in enrollment['attendance'] for day in selected),
            'attendance_on_closed_days': invalid,
        })
    return result


def require_valid_attendance(catalog):
    for enrollment in catalog.enrollments.values():
        provider = catalog.providers[enrollment['provider']]
        for day in enrollment['attendance']:
            if not is_open(provider, day):
                raise InvalidInput('attendance', f'{enrollment["id"]} attends closed day {day}')


def capacity_calendar(catalog, start, end):
    rows = []
    for day in days(start, end):
        for provider_id, provider in sorted(catalog.providers.items()):
            open_day = is_open(provider, day)
            enrolled = [row for row in catalog.enrollments.values() if row['provider'] == provider_id and row['start'] <= day < row['end']]
            present = sum(day in row['attendance'] for row in enrolled)
            allocated = len(enrolled) if open_day else 0
            rows.append({
                'day': day,
                'provider': provider_id,
                'open': open_day,
                'capacity': provider['capacity'] if open_day else 0,
                'enrolled': allocated,
                'attendance': present,
                'enrollment_excess': max(0, allocated - provider['capacity']) if open_day else 0,
                'attendance_excess': max(0, present - provider['capacity']) if open_day else present,
            })
    return rows
