from tariffs.primitives import integer, word, age_months


DEFAULT_INCOME_BANDS = [
    {'id': 'low', 'minimum': 0, 'maximum': 3000000},
    {'id': 'middle', 'minimum': 3000000, 'maximum': 10000000},
    {'id': 'high', 'minimum': 10000000, 'maximum': None},
]


def validate_bands(bands):
    if not isinstance(bands, list) or not bands:
        raise ValueError('expected nonempty income bands')
    expected = 0
    identifiers = set()
    for index, band in enumerate(bands):
        if not isinstance(band, dict) or set(band) != {'id', 'minimum', 'maximum'}:
            raise ValueError('income band requires id, minimum, maximum')
        word(band['id'], 'band.id')
        if band['id'] in identifiers:
            raise ValueError('duplicate income band id')
        identifiers.add(band['id'])
        integer(band['minimum'], 'band.minimum')
        if band['minimum'] != expected:
            raise ValueError('income bands must partition nonnegative incomes without gaps')
        maximum = band['maximum']
        if maximum is None:
            if index != len(bands) - 1:
                raise ValueError('only final income band may be open')
        else:
            integer(maximum, 'band.maximum', band['minimum'] + 1)
        expected = maximum
    if bands[-1]['maximum'] is not None:
        raise ValueError('final income band must be unbounded')
    return bands


def income_band(income, bands=None):
    if income is None:
        return 'unknown'
    bands = validate_bands(bands or DEFAULT_INCOME_BANDS)
    integer(income, 'income')
    for band in bands:
        if income >= band['minimum'] and (band['maximum'] is None or income < band['maximum']):
            return band['id']
    raise AssertionError('income not covered by validated partition')


def child_age_band(child, day):
    age = age_months(child['birth'], day)
    if age < 0:
        return 'not_born'
    if age < 12:
        return 'under_12_months'
    if age < 24:
        return '12_to_23_months'
    if age < 36:
        return '24_to_35_months'
    return '36_months_and_over'


def income_interval_bands(household, bands=None):
    low, high = household['income_low'], household['income_high']
    if low is None:
        return {'low_band': 'unknown', 'high_band': 'unknown', 'crosses_bands': False}
    first = income_band(low, bands)
    last = income_band(high, bands)
    return {'low_band': first, 'high_band': last, 'crosses_bands': first != last}
