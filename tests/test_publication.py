import copy
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from tariffs.catalog import Catalog
from tariffs.primitives import digest
from analysis.engine import evaluate
from publication.disclosure import safe_groups, publication_packet, validate_packet
from publication.query import families, encode, decode
from publication.tabular import SCHEMAS, read_table, write_table, read_directory, write_directory, convert
from publication.intake import apply_patch, difference, merge_tables
from publication import bundle
from publication.report_csv import safe_cell, render, export


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(Path('fixtures/county.json').read_text())
        self.catalog = Catalog(self.data)
        self.result = evaluate(self.catalog, '2026-11-01', '2026-12-01')
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_small_groups_and_complements(self):
        report = safe_groups(self.result, ['region'], 3)
        self.assertFalse(report['totals_publishable'])
        self.assertEqual(len(report['rows']), 2)
        self.assertTrue(all(row['suppressed'] for row in report['rows']))
        for row in report['rows']:
            self.assertEqual(set(row), {'region', 'suppressed', 'reason'})
        safe = safe_groups(self.result, ['region'], 2)
        self.assertTrue(safe['totals_publishable'])
        self.assertTrue(all(not row['suppressed'] for row in safe['rows']))
        packet = publication_packet(self.result, 3)
        total = next(report for report in packet['reports'] if not report['dimensions'])
        self.assertFalse(total['totals_publishable'])
        self.assertEqual(total['rows'][0]['reason'], 'subgroup_protection_requires_hidden_total')
        self.assertTrue(validate_packet(packet))
        self.assertFalse(packet['individual_rows_included'])

    def test_disclosure_counts_distinct_families(self):
        result = evaluate(self.catalog, '2026-11-01', '2027-01-01')
        report = safe_groups(result, [], 5)
        self.assertTrue(report['rows'][0]['suppressed'])
        report = safe_groups(result, [], 4)
        self.assertFalse(report['rows'][0]['suppressed'])
        self.assertEqual(report['rows'][0]['household_months'], 4)
        with self.assertRaises(ValueError):
            safe_groups(self.result, [], 0)
        forged = publication_packet(self.result)
        forged['individual_rows_included'] = True
        forged['packet_digest'] = digest({k: v for k, v in forged.items() if k != 'packet_digest'})
        with self.assertRaisesRegex(ValueError, 'individual'):
            validate_packet(forged)

    def test_cursor_pages_and_projection(self):
        page = families(self.result, columns=['household', 'month'], limit=2)
        self.assertEqual(page['total'], 5)
        self.assertEqual(page['offset'], 0)
        self.assertEqual(page['rows'], [{'household': 'h', 'month': '2026-11'}, {'household': 'i', 'month': '2026-11'}])
        second = families(self.result, columns=['household', 'month'], limit=2, cursor=page['next_cursor'])
        self.assertEqual(second['offset'], 2)
        self.assertEqual(len(second['rows']), 2)
        third = families(self.result, columns=['household', 'month'], limit=2, cursor=second['next_cursor'])
        self.assertEqual(third['rows'][0]['household'], 'zero')
        self.assertIsNone(third['next_cursor'])
        with self.assertRaises(ValueError):
            families(self.result, columns=['household'], cursor=page['next_cursor'])
        with self.assertRaises(ValueError):
            families(self.result, columns=['household', 'month'], filters={'region': ['east']}, cursor=page['next_cursor'])
        token = decode(page['next_cursor'])
        self.assertEqual(token['offset'], 2)
        self.assertEqual(decode(encode(token)), token)

    def test_query_invalid_parameters(self):
        with self.assertRaises(ValueError):
            families(self.result, filters={'child': ['h0']})
        with self.assertRaises(ValueError):
            families(self.result, filters={'household': ['h', 'h']})
        with self.assertRaises(ValueError):
            families(self.result, columns=[])
        with self.assertRaises(ValueError):
            families(self.result, columns=['secret'])
        with self.assertRaises(ValueError):
            families(self.result, limit=True)
        with self.assertRaises(ValueError):
            decode('!bad!')
        with self.assertRaises(ValueError):
            decode(encode([]))
        selected = families(self.result, filters={'household': ['h']}, columns=['household', 'burden'])
        self.assertEqual(selected['total'], 1)
        self.assertEqual(selected['rows'][0]['burden'], '0.388000000000')
        self.assertIsNone(selected['next_cursor'])

    def test_csv_all_tables_roundtrip(self):
        for table in SCHEMAS:
            with self.subTest(table=table):
                text = write_table(self.data, table)
                rows = read_table(text, table)
                self.assertEqual(rows, self.data[table])
                header = next(csv.reader(io.StringIO(text)))
                self.assertEqual(header, list(SCHEMAS[table]))
        target = self.root / 'csv'
        written = write_directory(self.data, target)
        self.assertEqual(len(written['files']), 5)
        restored = read_directory(target)
        self.assertEqual(restored.fingerprint, self.catalog.fingerprint)
        self.assertEqual(restored.document, self.data)
        with self.assertRaises(FileExistsError):
            write_directory(self.data, target)

    def test_csv_strict_input(self):
        table = write_table(self.data, 'providers')
        with self.assertRaises(ValueError):
            read_table(table.replace('capacity', 'unknown'), 'providers')
        with self.assertRaises(ValueError):
            read_table(table + 'too,few\n', 'providers')
        with self.assertRaises(ValueError):
            read_table('id,id\na,b\n', 'providers')
        with self.assertRaises(ValueError):
            read_table(table, 'unknown')
        with self.assertRaises(ValueError):
            convert('1.0', 'int')
        with self.assertRaises(ValueError):
            convert('１', 'int')
        with self.assertRaises(ValueError):
            convert('TRUE', 'bool')
        self.assertEqual(convert('true', 'bool'), True)
        self.assertIsNone(convert('', 'nullable_int'))
        self.assertEqual(convert('0002', 'int'), 2)
        target = self.root / 'csv'
        write_directory(self.data, target)
        (target / 'extra').write_text('unexpected')
        with self.assertRaises(ValueError):
            read_directory(target)

    def test_csv_symlink_refused(self):
        target = self.root / 'csv'
        write_directory(self.data, target)
        alias = self.root / 'alias'
        alias.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            read_directory(alias)
        original = target / 'providers.csv'
        moved = self.root / 'providers.csv'
        original.rename(moved)
        original.symlink_to(moved)
        with self.assertRaises(ValueError):
            read_directory(target)
        with self.assertRaises(FileExistsError):
            write_directory(self.data, alias)

    def test_atomic_patch_and_diff(self):
        row = {**self.data['providers'][0], 'capacity': 10}
        operations = [{'table': 'providers', 'action': 'replace', 'key': ['p'], 'row': row}]
        revised, report = apply_patch(self.catalog, operations)
        self.assertEqual(revised.providers['p']['capacity'], 10)
        self.assertEqual(self.catalog.providers['p']['capacity'], 3)
        self.assertTrue(report['atomic_validation'])
        self.assertEqual(report['before_digest'], self.catalog.fingerprint)
        self.assertEqual(report['after_digest'], revised.fingerprint)
        self.assertEqual(report['changes'][0]['after_digest'], digest(row))
        delta = difference(self.catalog, revised)
        self.assertEqual(delta['operations'], operations)
        reproduced, evidence = apply_patch(self.catalog, delta['operations'])
        self.assertEqual(reproduced.document, revised.document)
        self.assertEqual(evidence['after_digest'], report['after_digest'])
        self.assertTrue(delta['array_order_not_preserved'])

    def test_patch_referential_integrity(self):
        with self.assertRaises(ValueError):
            apply_patch(self.catalog, [{'table': 'providers', 'action': 'remove', 'key': ['p']}])
        self.assertIn('p', self.catalog.providers)
        first = self.data['enrollments'][0]
        changed = {**first, 'provider': 'q', 'attendance': []}
        revised, report = apply_patch(self.catalog, [{'table': 'enrollments', 'action': 'replace', 'key': [first['id']], 'row': changed}])
        self.assertEqual(revised.enrollments[first['id']]['provider'], 'q')
        self.assertEqual(len(report['changes']), 1)
        with self.assertRaises(ValueError):
            apply_patch(self.catalog, [{'table': 'providers', 'action': 'add', 'key': ['p'], 'row': self.data['providers'][0]}])
        with self.assertRaises(ValueError):
            apply_patch(self.catalog, [{'table': 'providers', 'action': 'replace', 'key': ['x'], 'row': self.data['providers'][0]}])
        with self.assertRaises(ValueError):
            apply_patch(self.catalog, [{'table': 'providers', 'action': 'remove', 'key': ['q'], 'row': {}}])
        with self.assertRaises(ValueError):
            apply_patch(self.catalog, [{'table': 'fees', 'action': 'remove', 'key': ['care']}])
        with self.assertRaises(ValueError):
            apply_patch(self.catalog, [{'table': 'providers', 'action': 'remove', 'key': ['q']}] * 2)

    def test_table_merge_and_joint_removal(self):
        revised, report = merge_tables(self.catalog, {'providers': write_table(self.data, 'providers')})
        self.assertEqual(revised.fingerprint, self.catalog.fingerprint)
        self.assertEqual(report['row_counts']['providers'], 2)
        with self.assertRaises(ValueError):
            merge_tables(self.catalog, {'providers': write_table(self.data, 'providers')}, 'append')
        with self.assertRaises(ValueError):
            merge_tables(self.catalog, {}, 'replace')
        with self.assertRaises(ValueError):
            merge_tables(self.catalog, {'unknown': ''})
        operations = [{'table': 'enrollments', 'action': 'remove', 'key': [row['id']]} for row in self.data['enrollments']]
        operations.extend({'table': 'fees', 'action': 'remove', 'key': [row['provider'], row['id'], row['start']]} for row in self.data['fees'])
        operations.append({'table': 'providers', 'action': 'remove', 'key': ['p']})
        revised, report = apply_patch(self.catalog, operations)
        self.assertNotIn('p', revised.providers)
        self.assertEqual(revised.enrollments, {})
        self.assertEqual(revised.fees, {})

    def test_bundle_roundtrip_and_tampering(self):
        target = self.root / 'bundle'
        evidence = bundle.write(self.catalog, self.result, target)
        self.assertTrue(evidence['verified'])
        self.assertEqual(evidence['files'], 10)
        self.assertEqual(bundle.verify(target), evidence)
        with self.assertRaises(FileExistsError):
            bundle.write(self.catalog, self.result, target)
        path = target / 'providers.json'
        original = path.read_text()
        path.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            bundle.verify(target)
        path.write_text(original)
        self.assertTrue(bundle.verify(target)['verified'])
        (target / 'extra.txt').write_text('extra')
        with self.assertRaises(ValueError):
            bundle.verify(target)

    def test_bundle_recomputed_hash_cannot_hide_wrong_derivation(self):
        target = self.root / 'bundle'
        bundle.write(self.catalog, self.result, target)
        path = target / 'audit.json'
        path.write_text('{}')
        import hashlib
        manifest_path = target / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['files']['audit.json'] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'derived'):
            bundle.verify(target)
        outside = self.root / 'outside'
        outside.write_text('x')
        manifest['files']['../outside'] = hashlib.sha256(b'x').hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            bundle.verify(target)

    def test_report_csv_formula_and_schema(self):
        self.assertEqual(safe_cell('=SUM(A1)'), "'=SUM(A1)")
        self.assertEqual(safe_cell(' +cmd'), "' +cmd")
        self.assertEqual(safe_cell(12), 12)
        self.assertEqual(safe_cell(True), 'true')
        self.assertEqual(safe_cell(None), '')
        self.assertEqual(safe_cell({'a': 1}), '{"a":1}')
        text = render([{'name': '=bad', 'amount': 1}], ['name', 'amount'])
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows[1], ["'=bad", '1'])
        with self.assertRaises(ValueError):
            render([{'name': 'x', 'extra': 1}], ['name'])
        with self.assertRaises(ValueError):
            render([], ['name', 'name'])
        for kind in ['fees', 'waterfall', 'burden-bands', 'region-gaps', 'groups']:
            report = export(self.result, kind)
            lines = list(csv.reader(io.StringIO(report['content'])))
            self.assertEqual(len(lines) - 1, report['rows'])
            self.assertTrue(report['spreadsheet_formula_cells_escaped'])
            self.assertEqual(report['result_digest'], self.result['result_digest'])
        with self.assertRaises(ValueError):
            export(self.result, 'household-secrets')


if __name__ == '__main__':
    unittest.main()
