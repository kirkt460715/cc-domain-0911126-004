import copy
from tariffs.catalog import Catalog
from tariffs.primitives import fields, integer, digest
from analysis.engine import evaluate
from analysis.groups import grouped


def change(document, modifications):
    catalog = Catalog(document)
    data = catalog.export()
    if not isinstance(modifications, list) or len(modifications) > 1000:
        raise ValueError('expected bounded modification list')
    seen = set()
    allowed = {
        'households': {'income_low', 'income_high', 'weight'},
        'providers': {'capacity', 'closed', 'extra_open'},
        'subsidies': {'value', 'income_ceiling', 'family_month_cap', 'priority'},
    }
    for modification in modifications:
        fields(modification, ('table', 'id', 'field', 'value'))
        table, ident, field = (modification[key] for key in ('table', 'id', 'field'))
        if table not in allowed or field not in allowed[table]:
            raise ValueError('unsupported scenario field')
        key = table, ident, field
        if key in seen:
            raise ValueError('duplicate scenario target')
        seen.add(key)
        matches = [row for row in data[table] if row['id'] == ident]
        if len(matches) != 1:
            raise ValueError('unknown scenario target')
        matches[0][field] = copy.deepcopy(modification['value'])
    return Catalog(data)


def paired_families(before, after):
    if before['start'] != after['start'] or before['end'] != after['end']:
        raise ValueError('paired analysis requires matching report windows')
    old = {(r['household'], r['month']): r for r in before['families']}
    new = {(r['household'], r['month']): r for r in after['families']}
    rows = []
    unpaired = []
    for key in sorted(old.keys() | new.keys()):
        if key not in old or key not in new:
            unpaired.append({'household': key[0], 'month': key[1]})
            continue
        a, b = old[key], new[key]
        rows.append({
            'household': key[0],
            'month': key[1],
            'before_net_cents': a['net_operating_cents'],
            'after_net_cents': b['net_operating_cents'],
            'net_change_cents': b['net_operating_cents'] - a['net_operating_cents'],
            'subsidy_change_cents': b['subsidy_cents'] - a['subsidy_cents'],
            'before_burden': a['burden'],
            'after_burden': b['burden'],
            'income_changed': a['period_income_cents'] != b['period_income_cents'],
            'weight_changed': a['weight'] != b['weight'],
        })
    return {'before_digest': before['result_digest'], 'after_digest': after['result_digest'], 'rows': rows, 'unpaired': unpaired}


def sensitivity(catalog, start, end, cases):
    if not isinstance(cases, list) or len(cases) > 100:
        raise ValueError('expected 0..100 sensitivity cases')
    names = set()
    results = []
    for case in cases:
        fields(case, ('name', 'endpoint', 'missing'), ('assumed_income', 'changes'))
        if not isinstance(case['name'], str) or not case['name'] or case['name'] in names:
            raise ValueError('sensitivity names must be unique nonempty strings')
        names.add(case['name'])
        variant = change(catalog.document, case.get('changes', []))
        result = evaluate(variant, start, end, case['endpoint'], case['missing'], case.get('assumed_income'))
        results.append({
            'name': case['name'],
            'input_digest': variant.fingerprint,
            'result_digest': result['result_digest'],
            'totals': result['totals'],
            'by_income': grouped(result, ['income_band']),
            'by_region': grouped(result, ['region']),
        })
    return {'baseline_input_digest': catalog.fingerprint, 'cases': results, 'interpretation': 'descriptive_assumption_sensitivity_not_causal_policy_effect'}
