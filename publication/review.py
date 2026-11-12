import json
from tariffs.primitives import canonical, digest, word, integer
from publication.store import Conflict
from analysis.quality import audit_result
from tariffs.catalog import Catalog


class ReviewDesk:
    def __init__(self, archive):
        self.archive = archive
        self.db = archive.db
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS review_cases(
            id TEXT PRIMARY KEY,
            analysis TEXT NOT NULL REFERENCES analyses(id),
            result_digest TEXT NOT NULL,
            body TEXT NOT NULL,
            body_digest TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('open','accepted','rejected')),
            revision INTEGER NOT NULL,
            decision TEXT
        );
        CREATE TABLE IF NOT EXISTS review_events(
            case_id TEXT NOT NULL REFERENCES review_cases(id),
            revision INTEGER NOT NULL,
            body TEXT NOT NULL,
            checksum TEXT NOT NULL,
            PRIMARY KEY(case_id,revision)
        );
        ''')

    def create(self, ident, analysis, reviewer, note):
        for key, value in [('id', ident), ('reviewer', reviewer), ('note', note)]:
            word(value, key)
        run = self.archive.analysis(analysis)
        if run['state'] != 'complete':
            raise Conflict('review requires complete analysis')
        source = Catalog(self.archive.input(run['input'])['document'])
        checked = audit_result(source, run['result'], replay=True)
        if not checked['valid']:
            raise ValueError('review requires consistent replay')
        body = {'analysis': analysis, 'result_digest': run['result']['result_digest'], 'reviewer': reviewer, 'note': note, 'audit': checked}
        with self.archive.atomic():
            existing = self.db.execute('SELECT * FROM review_cases WHERE id=?', (ident,)).fetchone()
            if existing:
                if existing['body_digest'] != digest(body):
                    raise Conflict('review case id binds another request')
            else:
                self.db.execute('INSERT INTO review_cases VALUES(?,?,?,?,?,?,?,?)', (ident, analysis, body['result_digest'], canonical(body), digest(body), 'open', 1, None))
                self._event(ident, 1, {'action': 'opened', 'body_digest': digest(body)})
        return self.case(ident)

    def _event(self, ident, revision, body):
        previous = self.db.execute('SELECT checksum FROM review_events WHERE case_id=? ORDER BY revision DESC LIMIT 1', (ident,)).fetchone()
        payload = {'case': ident, 'revision': revision, 'previous': previous[0] if previous else None, **body}
        self.db.execute('INSERT INTO review_events VALUES(?,?,?,?)', (ident, revision, canonical(payload), digest(payload)))

    def case(self, ident):
        row = self.db.execute('SELECT * FROM review_cases WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise KeyError(ident)
        item = dict(row)
        item['body'] = json.loads(item['body'])
        if digest(item['body']) != item['body_digest']:
            raise ValueError('review body integrity failure')
        if item['decision'] is not None:
            item['decision'] = json.loads(item['decision'])
        run = self.archive.analysis(item['analysis'])
        if run['result']['result_digest'] != item['result_digest']:
            raise ValueError('review result binding mismatch')
        return item

    def decide(self, ident, revision, state, actor, reason):
        integer(revision, 'revision', 1)
        word(actor, 'actor')
        word(reason, 'reason')
        if state not in ('accepted', 'rejected'):
            raise ValueError('invalid review decision')
        with self.archive.atomic():
            case = self.case(ident)
            if case['state'] != 'open' or case['revision'] != revision:
                raise Conflict('review case is closed or stale')
            if actor == case['body']['reviewer']:
                raise Conflict('case opener cannot approve their own review')
            decision = {'state': state, 'actor': actor, 'reason': reason, 'result_digest': case['result_digest']}
            self.db.execute('UPDATE review_cases SET state=?,revision=revision+1,decision=? WHERE id=?', (state, canonical(decision), ident))
            self._event(ident, revision + 1, {'action': 'decided', 'decision': decision})
        return self.case(ident)

    def history(self, ident):
        case = self.case(ident)
        previous = None
        events = []
        for index, row in enumerate(self.db.execute('SELECT * FROM review_events WHERE case_id=? ORDER BY revision', (ident,)), 1):
            body = json.loads(row['body'])
            if row['revision'] != index or body['revision'] != index or body['case'] != ident:
                raise ValueError('review event sequence mismatch')
            if body['previous'] != previous or digest(body) != row['checksum']:
                raise ValueError('review event chain mismatch')
            previous = row['checksum']
            events.append(body)
        if len(events) != case['revision']:
            raise ValueError('review case/event revision mismatch')
        if not events or events[0]['body_digest'] != case['body_digest']:
            raise ValueError('review opening event mismatch')
        if case['state'] == 'open' and (case['decision'] is not None or len(events) != 1):
            raise ValueError('open review has decision state')
        if case['state'] != 'open' and (case['decision'] is None or case['decision']['state'] != case['state']):
            raise ValueError('review state and decision disagree')
        if case['state'] != 'open' and events[-1].get('decision') != case['decision']:
            raise ValueError('review decision/event mismatch')
        return {'case': ident, 'events': events, 'local_integrity_only': True}

    def release(self, ident, label, minimum_households=3):
        case = self.case(ident)
        self.history(ident)
        if case['state'] != 'accepted':
            raise Conflict('review has not been accepted')
        return self.archive.release(label, case['analysis'], minimum_households)

    def pending(self):
        ids = [row[0] for row in self.db.execute("SELECT id FROM review_cases WHERE state='open' ORDER BY id")]
        return [self.case(ident) for ident in ids]

    def audit(self):
        ids = [row[0] for row in self.db.execute('SELECT id FROM review_cases ORDER BY id')]
        for ident in ids:
            self.history(ident)
        return {'valid': True, 'cases': len(ids), 'pending': len(self.pending())}
