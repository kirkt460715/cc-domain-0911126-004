import copy
from analysis.groups import grouped
from tariffs.primitives import integer, digest


def safe_groups(result, dimensions, minimum_households=3, population='enrolled'):
    integer(minimum_households, 'minimum_households', 1, 100000)
    internal = grouped(result, dimensions, population)
    rows = copy.deepcopy(internal['rows'])
    suppressed = []
    for index, row in enumerate(rows):
        if row['distinct_households'] < minimum_households:
            suppressed.append(index)
    if len(suppressed) == 1 and len(rows) > 1:
        candidates = [index for index in range(len(rows)) if index not in suppressed]
        secondary = min(candidates, key=lambda index: (rows[index]['distinct_households'], index))
        suppressed.append(secondary)
    output = []
    for index, row in enumerate(rows):
        if index in suppressed:
            public = {key: row[key] for key in dimensions}
            public.update({'suppressed': True, 'reason': 'small_sample_or_complementary_protection'})
        else:
            public = {**row, 'suppressed': False, 'reason': None}
        output.append(public)
    return {
        'result_digest': result['result_digest'],
        'dimensions': dimensions,
        'population': population,
        'minimum_distinct_households': minimum_households,
        'rows': output,
        'totals_publishable': not suppressed,
        'suppression_policy': 'suppress_small_groups_and_a_second_group_when_exactly_one_hidden',
        'cross_release_composition_not_guaranteed': True,
    }


def publication_packet(result, minimum_households=3):
    partitions = [[], ['region'], ['income_band'], ['month']]
    reports = [safe_groups(result, dimensions, minimum_households) for dimensions in partitions]
    if any(not report['totals_publishable'] for report in reports):
        for report in reports:
            if report['dimensions'] == []:
                report['rows'] = [{'suppressed': True, 'reason': 'subgroup_protection_requires_hidden_total'}]
                report['totals_publishable'] = False
    body = {
        'version': 1,
        'result_digest': result['result_digest'],
        'minimum_households': minimum_households,
        'reports': reports,
        'individual_rows_included': False,
        'limitations': 'separate_partitions_with_suppression_do_not_prove_arbitrary_cross_release_privacy',
    }
    return {**body, 'packet_digest': digest(body)}


def validate_packet(packet):
    body = {key: value for key, value in packet.items() if key != 'packet_digest'}
    if digest(body) != packet.get('packet_digest'):
        raise ValueError('publication packet checksum mismatch')
    if packet['individual_rows_included']:
        raise ValueError('public packet contains individual rows')
    for report in packet['reports']:
        hidden = [row for row in report['rows'] if row['suppressed']]
        if report['totals_publishable'] and hidden:
            raise ValueError('publishable total with suppressed cells')
        for row in hidden:
            if set(row) - set(report['dimensions']) - {'suppressed', 'reason'}:
                raise ValueError('suppressed group retains measures')
    return True
