from decimal import Decimal
from tariffs.primitives import integer


def observations(values):
    result = []
    for value, weight in values:
        integer(weight, 'weight', 1)
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError('observation must be finite')
        result.append((number, weight))
    return result


def mean(values):
    rows = observations(values)
    weight = sum(weight for value, weight in rows)
    if not weight:
        return None
    return sum(value * weight for value, weight in rows) / weight


def quantile(values, probability):
    probability = Decimal(str(probability))
    if not probability.is_finite() or not 0 <= probability <= 1:
        raise ValueError('quantile probability must be in 0..1')
    rows = sorted(observations(values))
    total = sum(weight for value, weight in rows)
    if not total:
        return None
    threshold = probability * total
    cumulative = 0
    for value, weight in rows:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return rows[-1][0]


def threshold_share(values, threshold, strict=True):
    threshold = Decimal(str(threshold))
    rows = observations(values)
    total = sum(weight for value, weight in rows)
    if not total:
        return None
    selected = sum(weight for value, weight in rows if value > threshold or not strict and value == threshold)
    return Decimal(selected) / total


def gini(values):
    rows = sorted(observations(values))
    if any(value < 0 for value, weight in rows):
        raise ValueError('gini requires nonnegative observations')
    total_weight = sum(weight for value, weight in rows)
    total_value = sum(value * weight for value, weight in rows)
    if not total_weight or not total_value:
        return None
    cumulative_weight = 0
    cumulative_value = Decimal(0)
    difference = Decimal(0)
    for value, weight in rows:
        difference += weight * (value * cumulative_weight - cumulative_value)
        cumulative_weight += weight
        cumulative_value += value * weight
    return difference / (total_weight * total_value)


def effective_sample_size(weights):
    for weight in weights:
        integer(weight, 'weight', 1)
    if not weights:
        return None
    total = sum(weights)
    return Decimal(total * total) / sum(weight * weight for weight in weights)


def trimmed_mean(values, fraction):
    fraction = Decimal(str(fraction))
    if not fraction.is_finite() or not 0 <= fraction < Decimal('0.5'):
        raise ValueError('trimming must be in [0,0.5)')
    rows = sorted(observations(values))
    total = sum(weight for value, weight in rows)
    if not total:
        return None
    lower = fraction * total
    upper = (1 - fraction) * total
    position = Decimal(0)
    numerator = Decimal(0)
    denominator = Decimal(0)
    for value, weight in rows:
        right = position + weight
        overlap = max(Decimal(0), min(right, upper) - max(position, lower))
        numerator += value * overlap
        denominator += overlap
        position = right
    return numerator / denominator if denominator else None


def display(value):
    return None if value is None else format(value, '.12f')


def summary(values):
    rows = observations(values)
    return {
        'observations': len(rows),
        'weight': sum(weight for value, weight in rows),
        'weighted_mean': display(mean(rows)),
        'p10': display(quantile(rows, '0.1')),
        'median': display(quantile(rows, '0.5')),
        'p90': display(quantile(rows, '0.9')),
        'effective_sample_size': display(effective_sample_size([w for v, w in rows])),
    }
