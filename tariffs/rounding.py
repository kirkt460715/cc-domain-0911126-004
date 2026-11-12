from decimal import Decimal, ROUND_FLOOR
from tariffs.primitives import cents, integer


def allocate_exact(values):
    if any(not value.is_finite() or value < 0 for value in values.values()):
        raise ValueError('allocation requires nonnegative finite decimals')
    floors = {key: int(value.to_integral_value(rounding=ROUND_FLOOR)) for key, value in values.items()}
    target = cents(sum(values.values(), Decimal(0)))
    remainder = target - sum(floors.values())
    order = sorted(values, key=lambda key: (-(values[key] - floors[key]), key))
    for key in order[:remainder]:
        floors[key] += 1
    if sum(floors.values()) != target:
        raise AssertionError('cent allocation does not conserve total')
    return floors


def capped_proportional(requests, cap):
    integer(cap, 'cap')
    for key, value in requests.items():
        integer(value, 'request')
    total = sum(requests.values())
    if total <= cap:
        return dict(requests)
    exact = {key: Decimal(value) * Decimal(cap) / Decimal(total) for key, value in requests.items()}
    allocated = allocate_exact(exact)
    if sum(allocated.values()) != cap:
        raise AssertionError('family cap allocation did not conserve budget')
    if any(allocated[key] > requests[key] for key in requests):
        raise AssertionError('family cap exceeds requested subsidy')
    return allocated


def monthly_split(total, month_days):
    integer(total, 'total')
    if not month_days or len(month_days) != len(set(month_days)):
        raise ValueError('expected unique nonempty days')
    return allocate_exact({day: Decimal(total) / Decimal(len(month_days)) for day in month_days})
