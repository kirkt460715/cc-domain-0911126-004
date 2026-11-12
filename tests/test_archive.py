import copy
import http.client
from http.server import HTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from tariffs.catalog import Catalog
from publication.store import Archive, Conflict
from publication.service import Service
from publication.review import ReviewDesk
from publication import recovery
from publication.http import handler, serve
from nursery_report import demonstration, main


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(Path('fixtures/county.json').read_text())
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / 'archive.sqlite3'
        self.archive = Archive(self.path)
        self.service = Service(self.archive)
        self.archive.add_input('county', self.data)

    def tearDown(self):
        self.archive.close()
        self.temp.cleanup()

    def submit(self, ident='run'):
        return self.archive.submit(ident, 'county', '2026-11-01', '2026-12-01')

    def complete(self, ident='run'):
        self.submit(ident)
        return self.archive.execute(ident)

    def test_immutable_input_and_request(self):
        first = self.archive.input('county')
        second = self.archive.add_input('county', self.data)
        self.assertEqual(first, second)
        changed = copy.deepcopy(self.data)
        changed['providers'][0]['capacity'] += 1
        with self.assertRaises(Conflict):
            self.archive.add_input('county', changed)
        self.assertEqual(self.archive.input('county'), first)
        pending = self.submit()
        self.assertEqual(pending['state'], 'pending')
        self.assertEqual(pending['revision'], 1)
        self.assertEqual(self.submit(), pending)
        with self.assertRaises(Conflict):
            self.archive.submit('run', 'county', '2026-11-02', '2026-12-01')
        with self.assertRaises(KeyError):
            self.archive.input('missing')

    def test_execute_fencing(self):
        self.submit()
        other = Archive(self.path)
        try:
            snapshot = other.analysis('run')
            completed = self.archive.execute('run', snapshot['revision'])
            self.assertEqual(completed['state'], 'complete')
            self.assertEqual(completed['revision'], 2)
            self.assertEqual(other.analysis('run')['result'], completed['result'])
            with self.assertRaises(Conflict):
                other.execute('run', snapshot['revision'])
            with self.assertRaises(Conflict):
                other.retry('run', 2)
        finally:
            other.close()
        self.assertTrue(recovery.audit(self.archive)['valid'])
        self.assertEqual(recovery.audit(self.archive)['analyses'], 1)

    def test_failed_analysis_is_explicit_and_retryable(self):
        data = copy.deepcopy(self.data)
        data['providers'][0]['closed'] = ['2026-11-03']
        self.archive.add_input('bad-attendance', data)
        self.archive.submit('bad', 'bad-attendance', '2026-11-01', '2026-12-01')
        with self.assertRaises(ValueError):
            self.archive.execute('bad')
        failed = self.archive.analysis('bad')
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(failed['revision'], 2)
        self.assertIn('closed day', failed['error'])
        self.assertIsNone(failed['result'])
        with self.assertRaises(Conflict):
            self.archive.retry('bad', 1)
        retried = self.archive.retry('bad', 2)
        self.assertEqual(retried['state'], 'pending')
        self.assertEqual(retried['revision'], 3)
        with self.assertRaises(ValueError):
            self.archive.execute('bad', 3)
        self.assertEqual(self.archive.analysis('bad')['revision'], 4)
        self.assertTrue(recovery.audit(self.archive)['valid'])

    def test_release_is_immutable(self):
        self.submit()
        with self.assertRaises(Conflict):
            self.archive.release('public', 'run')
        self.archive.execute('run')
        first = self.archive.release('public', 'run', 3)
        second = self.archive.release('public', 'run', 3)
        self.assertEqual(first, second)
        with self.assertRaises(Conflict):
            self.archive.release('public', 'run', 2)
        self.assertEqual(self.archive.publication('public'), first)
        self.assertFalse(first['packet']['individual_rows_included'])
        self.assertEqual(recovery.audit(self.archive)['releases'], 1)
        with self.assertRaises(KeyError):
            self.archive.publication('missing')

    def test_backup_restore_and_checksum(self):
        self.complete()
        self.archive.release('public', 'run')
        backup = self.root / 'backup.sqlite3'
        evidence = recovery.backup(self.archive, backup)
        target = self.root / 'restored.sqlite3'
        report = recovery.restore(backup, target, evidence['digest'])
        self.assertTrue(report['audit']['valid'])
        with Archive(target) as restored:
            self.assertEqual(restored.analysis('run')['result'], self.archive.analysis('run')['result'])
            self.assertEqual(restored.publication('public'), self.archive.publication('public'))
        with self.assertRaises(FileExistsError):
            recovery.backup(self.archive, backup)
        with self.assertRaises(FileExistsError):
            recovery.restore(backup, target, evidence['digest'])
        with self.assertRaises(ValueError):
            recovery.restore(backup, self.root / 'bad.sqlite3', '0' * 64)
        self.assertFalse((self.root / 'bad.sqlite3').exists())

    def test_review_separation_revision_and_recovery(self):
        self.complete()
        desk = ReviewDesk(self.archive)
        opened = desk.create('case', 'run', 'alice', 'Check subsidy caps')
        self.assertEqual(opened['state'], 'open')
        self.assertEqual(desk.create('case', 'run', 'alice', 'Check subsidy caps'), opened)
        self.assertEqual(len(desk.pending()), 1)
        with self.assertRaises(Conflict):
            desk.decide('case', 1, 'accepted', 'alice', 'self approval')
        with self.assertRaises(Conflict):
            desk.decide('case', 2, 'accepted', 'bob', 'stale revision')
        with self.assertRaises(Conflict):
            desk.release('case', 'public')
        accepted = desk.decide('case', 1, 'accepted', 'bob', 'Replayed and reconciled')
        self.assertEqual(accepted['state'], 'accepted')
        self.assertEqual(accepted['revision'], 2)
        self.assertEqual(len(desk.history('case')['events']), 2)
        self.assertEqual(desk.pending(), [])
        published = desk.release('case', 'reviewed')
        self.assertEqual(published['analysis'], 'run')
        with self.assertRaises(Conflict):
            desk.decide('case', 2, 'rejected', 'carol', 'late change')
        evidence = recovery.backup(self.archive, self.root / 'review.sqlite3')
        self.assertTrue(evidence['audit']['valid'])
        with Archive(self.root / 'review.sqlite3') as restored:
            self.assertTrue(ReviewDesk(restored).audit()['valid'])
            self.assertEqual(ReviewDesk(restored).case('case')['decision'], accepted['decision'])

    def test_review_tampering_detected(self):
        self.complete()
        desk = ReviewDesk(self.archive)
        desk.create('case', 'run', 'alice', 'review')
        self.archive.db.execute("UPDATE review_events SET body='{}' WHERE case_id='case'")
        with self.assertRaises((ValueError, KeyError)):
            desk.history('case')
        with self.assertRaises((ValueError, KeyError)):
            recovery.audit(self.archive)

    def test_service_reports_and_commands(self):
        self.complete()
        names = ['missingness', 'fees', 'waterfall', 'reasons', 'providers', 'incidence', 'coverage', 'occupancy', 'age-transitions', 'change-points', 'concentration', 'region-gaps', 'program-overlap', 'leave-one-out', 'recipients', 'input-quality', 'audit', 'cross-region', 'attendance-costs', 'enrollment-movements', 'cost-by-age']
        for kind in names:
            with self.subTest(kind=kind):
                report = self.service.dispatch('report', {'id': 'run', 'kind': kind})
                self.assertIsNotNone(report)
        report = self.service.report('run', 'groups', {'dimensions': ['region']})
        self.assertEqual(len(report['rows']), 2)
        self.assertEqual(self.service.report('run', 'statement', {'household': 'h'})['household'], 'h')
        self.assertEqual(self.service.report('run', 'families', {'limit': 1})['total'], 5)
        self.assertEqual(self.service.report('run', 'csv', {'kind': 'waterfall'})['rows'], 2)
        self.assertEqual(self.service.report('run', 'income-grid', {'household': 'i'})['critical_income_cents'][1], 3000000)
        self.assertFalse(self.service.report('run', 'public', {})['individual_rows_included'])
        self.assertEqual(self.service.report('run', 'lorenz', {})['points'][-1]['population_share'], '1.000000000000')
        self.assertIsInstance(self.service.report('run', 'burden-bands', {}), list)
        self.assertEqual(len(self.service.report('run', 'budget-sweep', {'program': 'family', 'caps': [0, 1]})['rows']), 2)
        self.assertEqual(self.service.report('run', 'sensitivity', {'cases': []})['cases'], [])
        self.assertEqual(self.service.dispatch('list', {'table': 'inputs'})['ids'], ['county'])
        self.assertEqual(self.service.dispatch('compare', {'before': 'run', 'after': 'run'})['unpaired'], [])
        with self.assertRaises(ValueError):
            self.service.report('run', 'fees', {'extra': 1})
        with self.assertRaises(ValueError):
            self.service.dispatch('unknown', {})
        with self.assertRaises(ValueError):
            self.service.dispatch('list', {'table': 'sqlite_master'})

    def test_service_patch_and_review(self):
        row = {**self.data['providers'][0], 'capacity': 12}
        operations = [{'table': 'providers', 'action': 'replace', 'key': ['p'], 'row': row}]
        report = self.service.dispatch('patch', {'source': 'county', 'id': 'new', 'operations': operations})
        self.assertNotEqual(report['before_digest'], report['after_digest'])
        self.assertEqual(self.service.dispatch('diff', {'before': 'county', 'after': 'new'})['operations'], operations)
        self.service.dispatch('submit', {'id': 'run', 'input': 'county', 'start': '2026-11-01', 'end': '2026-12-01'})
        self.service.dispatch('execute', {'id': 'run'})
        self.service.dispatch('review-create', {'id': 'c', 'analysis': 'run', 'reviewer': 'a', 'note': 'ready'})
        self.assertEqual(len(self.service.dispatch('review-pending', {})), 1)
        self.service.dispatch('review-decide', {'id': 'c', 'revision': 1, 'state': 'accepted', 'actor': 'b', 'reason': 'checked'})
        self.assertEqual(self.service.dispatch('review-get', {'id': 'c'})['state'], 'accepted')
        self.assertEqual(len(self.service.dispatch('review-history', {'id': 'c'})['events']), 2)
        released = self.service.dispatch('review-release', {'id': 'c', 'label': 'release'})
        self.assertEqual(released['label'], 'release')
        self.assertEqual(self.service.dispatch('audit', {})['releases'], 1)

    def test_local_http_auth_and_protocol(self):
        token = 'local-test-token-with-24-characters'
        server = HTTPServer(('127.0.0.1', 0), handler(self.path, token))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(body, auth=True, route='/v1/get-input', content_type='application/json'):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            headers = {'Content-Type': content_type}
            if auth:
                headers['Authorization'] = 'Bearer ' + token
            connection.request('POST', route, body, headers)
            response = connection.getresponse()
            status = response.status
            output = json.loads(response.read())
            connection.close()
            return status, output
        try:
            status, body = request('{"id":"county"}')
            self.assertEqual(status, 200)
            self.assertEqual(body['result']['id'], 'county')
            self.assertEqual(request('{"id":"county"}', auth=False)[0], 401)
            self.assertEqual(request('{"id":"missing"}')[0], 404)
            self.assertEqual(request('not-json')[0], 400)
            self.assertEqual(request('{}', route='/unknown')[0], 404)
            self.assertEqual(request('{}', content_type='text/plain')[0], 415)
            self.assertEqual(request('{}', route='/v1/unknown')[0], 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
        with self.assertRaises(ValueError):
            handler(self.path, 'short')
        with self.assertRaises(ValueError):
            serve(self.path, token, host='0.0.0.0')

    def test_real_cli_demonstration(self):
        result = demonstration('fixtures/county.json', '2026-11-01', '2026-12-01')
        self.assertEqual(result['data'], 'local_synthetic_fixture')
        self.assertTrue(result['arithmetic_audit']['valid'])
        self.assertEqual(result['archive']['analyses'], 1)
        self.assertEqual(result['archive']['releases'], 1)
        self.assertTrue(result['bundle']['verified'])
        from contextlib import redirect_stderr
        from io import StringIO
        output = StringIO()
        with redirect_stderr(output):
            status = main(['evaluate', '--input', str(self.root / 'missing.json'), '--start', '2026-11-01', '--end', '2026-12-01'])
        self.assertEqual(status, 2)
        self.assertIn('error', json.loads(output.getvalue()))


if __name__ == '__main__':
    unittest.main()
