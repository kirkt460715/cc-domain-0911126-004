import csv
import io
from analysis.engine import verify
from analysis.groups import grouped
from analysis.explanations import fee_contributions, subsidy_waterfall
from analysis.distribution import burden_bands, region_gaps
from tariffs.primitives import canonical


def safe_cell(value):
    if value is None:
        return ''
    if isinstance(value, (dict, list)):
        return canonical(value)
    if type(value) is bool:
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    if text.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')):
        return "'" + text
    return text


def render(rows, columns):
    if not isinstance(rows, list) or not isinstance(columns, list) or not columns:
        raise ValueError('CSV report requires rows and columns')
    if len(set(columns)) != len(columns) or any(not isinstance(column, str) for column in columns):
        raise ValueError('CSV columns must be unique strings')
    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator='\n')
    writer.writerow(columns)
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(columns):
            raise ValueError('CSV report row differs from declared schema')
        writer.writerow([safe_cell(row[column]) for column in columns])
    return output.getvalue()


def export(result, kind, dimensions=None):
    verify(result)
    if kind == 'fees':
        rows = fee_contributions(result)
        columns = ['provider', 'fee', 'refundable', 'gross_cents', 'children', 'households', 'charged_months', 'operating_share']
    elif kind == 'waterfall':
        rows = subsidy_waterfall(result)
        columns = ['program', 'month', 'selected_requests_cents', 'after_family_cap_cents', 'family_cap_reduction_cents', 'after_child_cap_cents', 'child_cap_reduction_cents', 'recipient_children', 'recipient_households']
    elif kind == 'burden-bands':
        rows = burden_bands(result)
        columns = ['band', 'minimum', 'maximum_exclusive', 'household_months', 'distinct_households', 'weight', 'weighted_net_cents']
    elif kind == 'region-gaps':
        rows = region_gaps(result)
        columns = ['region', 'mean_burden', 'national_mean_burden', 'mean_gap', 'median_burden', 'effective_sample_size', 'known_ratio_household_months', 'missing_or_zero_income_household_months', 'interpretation']
    elif kind == 'groups':
        dimensions = [] if dimensions is None else dimensions
        rows = grouped(result, dimensions)['rows']
        measures = ['household_months', 'distinct_households', 'weight', 'income_known_weight', 'income_missing_weight', 'income_assumed_weight', 'positive_cost_zero_income_weight', 'weighted_net_cost_cents', 'weighted_subsidy_cents', 'ratio_of_totals', 'burden_summary', 'cost_summary', 'burden_above_threshold_share', 'burden_threshold', 'trimmed_mean_burden', 'net_cost_gini']
        columns = dimensions + measures
    else:
        raise ValueError('unsupported report CSV kind')
    return {
        'result_digest': result['result_digest'],
        'kind': kind,
        'rows': len(rows),
        'content': render(rows, columns),
        'access': 'restricted_aggregate_no_small_sample_protection',
        'spreadsheet_formula_cells_escaped': True,
    }
