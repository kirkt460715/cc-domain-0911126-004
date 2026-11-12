import json
from tariffs.catalog import Catalog
from tariffs.primitives import fields, canonical
from analysis.engine import evaluate
from analysis.groups import grouped, income_missingness
from analysis.quality import input_quality, audit_result
from analysis.explanations import family_statement, fee_contributions, subsidy_waterfall, eligibility_reasons
from analysis.distribution import lorenz, concentration, burden_bands, region_gaps
from analysis.policy_review import program_overlap, leave_one_program_out, budget_sweep, program_recipients
from analysis.providers import provider_report, subsidy_incidence
from analysis.income_grid import household_envelope
from analysis.comparison import compare, endpoint_envelope
from analysis.variants import change, sensitivity
from tariffs.coverage import fee_coverage, occupancy_summary, age_transitions, tariff_change_points
from publication.disclosure import publication_packet
from publication.query import families
from publication.store import Archive
from publication.intake import apply_patch, difference
from publication.recovery import audit


class Service:
    def __init__(self, archive):
        self.archive = archive

    def result(self, ident):
        run = self.archive.analysis(ident)
        if run['state'] != 'complete':
            raise ValueError('requested analysis is not complete')
        return run['result']

    def source(self, analysis):
        run = self.archive.analysis(analysis)
        return Catalog(self.archive.input(run['input'])['document'])

    def dispatch(self, command, body):
        if not isinstance(body, dict):
            raise ValueError('request must be an object')
        if command.startswith('review-'):
            from publication.review import ReviewDesk
            desk = ReviewDesk(self.archive)
            if command == 'review-create':
                fields(body, ('id', 'analysis', 'reviewer', 'note'))
                return desk.create(body['id'], body['analysis'], body['reviewer'], body['note'])
            if command == 'review-decide':
                fields(body, ('id', 'revision', 'state', 'actor', 'reason'))
                return desk.decide(body['id'], body['revision'], body['state'], body['actor'], body['reason'])
            if command == 'review-get':
                fields(body, ('id',))
                return desk.case(body['id'])
            if command == 'review-history':
                fields(body, ('id',))
                return desk.history(body['id'])
            if command == 'review-release':
                fields(body, ('id', 'label'), ('minimum_households',))
                return desk.release(body['id'], body['label'], body.get('minimum_households', 3))
            if command == 'review-pending':
                fields(body, ())
                return desk.pending()
            raise ValueError('unknown review command')
        if command == 'input':
            fields(body, ('id', 'document'))
            return self.archive.add_input(body['id'], body['document'])
        if command == 'get-input':
            fields(body, ('id',))
            return self.archive.input(body['id'])
        if command == 'patch':
            fields(body, ('source', 'id', 'operations'))
            source = Catalog(self.archive.input(body['source'])['document'])
            target, report = apply_patch(source, body['operations'])
            self.archive.add_input(body['id'], target.document)
            return report
        if command == 'diff':
            fields(body, ('before', 'after'))
            before = Catalog(self.archive.input(body['before'])['document'])
            after = Catalog(self.archive.input(body['after'])['document'])
            return difference(before, after)
        if command == 'submit':
            fields(body, ('id', 'input', 'start', 'end'), ('endpoint', 'missing', 'assumed_income'))
            return self.archive.submit(body['id'], body['input'], body['start'], body['end'], body.get('endpoint', 'low'), body.get('missing', 'exclude'), body.get('assumed_income'))
        if command == 'execute':
            fields(body, ('id',), ('revision',))
            return self.archive.execute(body['id'], body.get('revision', 1))
        if command == 'retry':
            fields(body, ('id', 'revision'))
            return self.archive.retry(body['id'], body['revision'])
        if command == 'get-analysis':
            fields(body, ('id',))
            return self.archive.analysis(body['id'])
        if command == 'release':
            fields(body, ('id', 'label'), ('minimum_households',))
            return self.archive.release(body['label'], body['id'], body.get('minimum_households', 3))
        if command == 'get-release':
            fields(body, ('label',))
            return self.archive.publication(body['label'])
        if command == 'audit':
            fields(body, ())
            return audit(self.archive)
        if command == 'list':
            fields(body, ('table',))
            allowed = {'inputs': 'id', 'analyses': 'id', 'releases': 'label'}
            table = body['table']
            if table not in allowed:
                raise ValueError('unknown archive collection')
            column = allowed[table]
            return {'ids': [row[0] for row in self.archive.db.execute(f'SELECT {column} FROM {table} ORDER BY {column}')]}
        if command == 'compare':
            fields(body, ('before', 'after'))
            return compare(self.result(body['before']), self.result(body['after']))
        if command == 'envelope':
            fields(body, ('low', 'high'))
            return endpoint_envelope(self.result(body['low']), self.result(body['high']))
        if command == 'report':
            fields(body, ('id', 'kind'), ('options',))
            return self.report(body['id'], body['kind'], body.get('options', {}))
        raise ValueError('unknown service command')

    def report(self, ident, kind, options):
        if not isinstance(options, dict):
            raise ValueError('report options must be an object')
        result = self.result(ident)
        catalog = self.source(ident)
        start, end = result['start'], result['end']
        from analysis.service_use import cross_region_flows, attendance_costs, enrollment_movements, cost_by_age
        simple = {
            'cross-region': lambda: cross_region_flows(catalog, result),
            'attendance-costs': lambda: attendance_costs(catalog, result),
            'enrollment-movements': lambda: enrollment_movements(catalog, start, end),
            'cost-by-age': lambda: cost_by_age(result),
            'missingness': lambda: income_missingness(result),
            'fees': lambda: fee_contributions(result),
            'waterfall': lambda: subsidy_waterfall(result),
            'reasons': lambda: eligibility_reasons(result),
            'providers': lambda: provider_report(catalog, result),
            'incidence': lambda: subsidy_incidence(catalog, result),
            'coverage': lambda: fee_coverage(catalog, start, end),
            'occupancy': lambda: occupancy_summary(catalog, start, end),
            'age-transitions': lambda: age_transitions(catalog, start, end),
            'change-points': lambda: tariff_change_points(catalog, start, end),
            'concentration': lambda: concentration(result),
            'region-gaps': lambda: region_gaps(result),
            'program-overlap': lambda: program_overlap(catalog, start, end),
            'leave-one-out': lambda: leave_one_program_out(catalog, result),
            'recipients': lambda: program_recipients(result),
            'input-quality': lambda: input_quality(catalog, start, end),
            'audit': lambda: audit_result(catalog, result, replay=True),
        }
        if kind in simple:
            fields(options, ())
            return simple[kind]()
        if kind == 'csv':
            from publication.report_csv import export
            fields(options, ('kind',), ('dimensions',))
            return export(result, options['kind'], options.get('dimensions'))
        if kind == 'reconcile':
            from analysis.reconciliation import reconcile
            fields(options, ('claims',), ('tolerance_cents',))
            return reconcile(catalog, result, options['claims'], options.get('tolerance_cents', 0))
        if kind == 'groups':
            fields(options, ('dimensions',), ('population', 'threshold'))
            return grouped(result, options['dimensions'], options.get('population', 'enrolled'), options.get('threshold', '.1'))
        if kind == 'families':
            fields(options, (), ('filters', 'columns', 'limit', 'cursor'))
            return families(result, **options)
        if kind == 'statement':
            fields(options, ('household',))
            return family_statement(catalog, result, options['household'])
        if kind == 'income-grid':
            fields(options, ('household',))
            return household_envelope(catalog, options['household'], start, end)
        if kind == 'public':
            fields(options, (), ('minimum_households',))
            return publication_packet(result, options.get('minimum_households', 3))
        if kind == 'lorenz':
            fields(options, (), ('metric', 'population'))
            return lorenz(result, **options)
        if kind == 'burden-bands':
            fields(options, (), ('boundaries',))
            return burden_bands(result, options.get('boundaries'))
        if kind == 'budget-sweep':
            fields(options, ('program', 'caps'))
            return budget_sweep(catalog, result, options['program'], options['caps'])
        if kind == 'sensitivity':
            fields(options, ('cases',))
            return sensitivity(catalog, start, end, options['cases'])
        raise ValueError('unknown report kind')
