import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from app.config import Settings
from app.database import Database
from app.rbac_guard import PERSONAS, ROLE_SCOPES
from app.sql_analytics import SQLAnalytics


METRICS = {
    'department_headcount': set(ROLE_SCOPES),
    'quarterly_budget_summary': {'admin', 'finance'},
    'sla_uptime_metrics': {'admin', 'engineering'},
    'compensation_band_summary': {'admin', 'hr'},
    'vendor_spend_rollup': {'admin', 'finance', 'hr'},
}


@pytest.fixture(scope='module')
def seeded():
    # Keep all test artifacts in this project, never system temporary directories.
    root = Path.cwd() / ('.sql-analytics-tests-' + uuid4().hex)
    root.mkdir()
    try:
        db = Database(Settings(data_dir=root / 'seed', jwt_secret='test-only-' * 5))
        db.initialize()
        yield db, root
    finally:
        shutil.rmtree(root)


@pytest.fixture
def database(seeded):
    source, root = seeded
    db = Database(Settings(data_dir=root / uuid4().hex, jwt_secret='test-only-' * 5))
    db.path.parent.mkdir(parents=True)
    with source.connect() as original, db.connect() as destination:
        original.backup(destination)
    return db


@pytest.mark.parametrize('metric_id', METRICS)
@pytest.mark.parametrize('uid,role', PERSONAS.items())
def test_metric_role_matrix_and_exact_scopes(database, uid, role, metric_id):
    analytics = SQLAnalytics(database)
    user = database.user(uid)
    user['scopes'] = list(ROLE_SCOPES['admin'])
    if role not in METRICS[metric_id]:
        with pytest.raises(PermissionError):
            analytics.query(metric_id, user)
        return
    result = analytics.query(metric_id, user)
    assert set(result) == {'metric_id', 'columns', 'rows', 'max_rows'}
    assert result['metric_id'] == metric_id
    assert result['columns'] == ['label', 'period', 'value', 'unit']
    assert result['max_rows'] == 50
    with database.connect() as conn:
        expected = [dict(row) for row in conn.execute(
            'SELECT label,period,value,unit,scope FROM analytics '
            'WHERE metric_id=? ORDER BY label,period,scope,value,unit', (metric_id,)
        ) if row['scope'] in ROLE_SCOPES[role]]
    for row in expected:
        row.pop('scope')
    assert result['rows'] == expected
    assert all(set(row) == set(result['columns']) for row in result['rows'])


@pytest.mark.parametrize('metric_id', [
    'unknown', 'SELECT * FROM users', "department_headcount'; DROP TABLE analytics;--", None, [],
])
def test_unknown_metric(database, metric_id):
    with pytest.raises(ValueError, match='Unknown metric'):
        SQLAnalytics(database).query(metric_id, {'role': 'admin'})


@pytest.mark.parametrize('user', [None, {}, {'role': 'unknown'}, {'role': []}, {'scopes': ['finance']}])
def test_invalid_identity_denied(database, user):
    with pytest.raises(PermissionError):
        SQLAnalytics(database).query('department_headcount', user)


def test_current_role_not_persona_or_supplied_scopes(database):
    database.change_role('admin_user', 'employee')
    user = database.user('admin_user')
    user['scopes'] = ["general') OR 1=1 --", 'finance']
    analytics = SQLAnalytics(database)
    with pytest.raises(PermissionError):
        analytics.query('quarterly_budget_summary', user)
    assert [row['label'] for row in analytics.query('department_headcount', user)['rows']] == ['General']


@pytest.mark.parametrize('period', ['2026-Q4', '2026', '', "2026-Q4' OR 1=1 --", "'; DROP TABLE analytics;--"])
def test_period_is_bound_and_exact(database, period):
    with database.connect() as conn:
        conn.execute('INSERT INTO analytics(metric_id,scope,label,period,value,unit) VALUES(?,?,?,?,?,?)',
                     ('department_headcount', 'general', 'Exact period', period, 1, 'people'))
    analytics = SQLAnalytics(database)
    rows = analytics.query('department_headcount', {'role': 'employee'}, period)['rows']
    assert rows
    assert all(row['period'] == period for row in rows)
    assert any(row['label'] == 'Exact period' for row in rows)
    with database.connect() as conn:
        assert conn.execute('SELECT count(*) FROM analytics').fetchone()[0] == 15


def test_unmatched_and_invalid_period(database):
    analytics = SQLAnalytics(database)
    assert analytics.query('department_headcount', {'role': 'admin'}, 'missing')['rows'] == []
    with pytest.raises(ValueError, match='Period'):
        analytics.query('department_headcount', {'role': 'admin'}, ['2026-Q4'])


def test_limit_applied_after_scope_filter(database):
    with database.connect() as conn:
        conn.executemany('INSERT INTO analytics(metric_id,scope,label,period,value,unit) VALUES(?,?,?,?,?,?)', [
            ('department_headcount', scope, f'{prefix}{index:03}', '2099', index, 'people')
            for scope, prefix in [('finance', 'A-secret-'), ('general', 'B-public-')]
            for index in range(80)
        ])
    analytics = SQLAnalytics(database)
    result = analytics.query('department_headcount', {'role': 'employee'}, '2099')
    assert len(result['rows']) == result['max_rows'] == 50
    assert [row['label'] for row in result['rows']] == [f'B-public-{index:03}' for index in range(50)]
    assert analytics.query('department_headcount', {'role': 'employee'}, '2099') == result
    assert len(analytics.query('department_headcount', {'role': 'admin'}, '2099')['rows']) == 50


@pytest.mark.parametrize('sql', [
    "INSERT INTO analytics(metric_id,scope,label,period,value,unit) VALUES('x','general','x','x',1,'x')",
    'UPDATE analytics SET value=0',
    'DELETE FROM analytics',
    'DROP TABLE analytics',
    "ATTACH DATABASE ':memory:' AS other",
    'DETACH DATABASE main',
    'PRAGMA table_info(analytics)',
    'PRAGMA query_only=OFF',
    'CREATE TABLE forbidden(value TEXT)',
    'CREATE TEMP TABLE forbidden(value TEXT)',
    'ALTER TABLE analytics ADD COLUMN forbidden TEXT',
    'CREATE INDEX forbidden ON analytics(label)',
    'SELECT sql FROM sqlite_master',
    'SELECT name FROM main.sqlite_schema',
    'SELECT name FROM sqlite_temp_master',
    'SELECT password_hash FROM users',
    'SELECT * FROM documents',
    'SELECT * FROM audit_logs',
    'SELECT * FROM meta',
    'SELECT id FROM analytics',
    'SELECT rowid FROM analytics',
    'SELECT * FROM analytics',
    'SELECT count(*) FROM analytics',
    'SELECT lower(label) FROM analytics',
    "SELECT load_extension('not-an-extension')",
    "SELECT * FROM pragma_table_info('analytics')",
    'BEGIN',
])
def test_authorizer_rejects_actual_statements(database, sql):
    analytics = SQLAnalytics(database)
    with analytics.open_readonly() as conn:
        with pytest.raises(sqlite3.DatabaseError) as error:
            conn.execute(sql).fetchall()
        # Distinguish authorizer rejection from read-only or syntax errors.
        assert error.value.sqlite_errorcode == sqlite3.SQLITE_AUTH or 'not authorized' in str(error.value)
        assert conn.execute('SELECT label FROM analytics LIMIT 1').fetchone()
    with database.connect() as conn:
        assert conn.execute('SELECT count(*) FROM analytics').fetchone()[0] == 14
        assert conn.execute('SELECT value FROM analytics WHERE label=?', ('General',)).fetchone()[0] == 12


def test_uri_readonly_even_without_authorizer(database):
    with SQLAnalytics(database).open_readonly() as conn:
        conn.set_authorizer(None)
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            conn.execute('UPDATE analytics SET value=0')


def test_uri_escapes_filename(database):
    target = database.path.with_name('metrics?#.sqlite3')
    database.path.rename(target)
    database.path = target
    assert SQLAnalytics(database).query('department_headcount', {'role': 'employee'})['rows']


def test_missing_database_is_not_created(database):
    database.path = database.path.with_name('missing.sqlite3')
    with pytest.raises(sqlite3.OperationalError):
        with SQLAnalytics(database).open_readonly():
            pytest.fail('Missing database unexpectedly opened')
    assert not database.path.exists()


def test_step_budget_aborts_expensive_select(database):
    analytics = SQLAnalytics(database)
    analytics.STEP_BUDGET = 500
    with analytics.open_readonly() as conn:
        with pytest.raises(sqlite3.OperationalError, match='interrupted') as error:
            conn.execute('SELECT a.label,b.label,c.label,d.label '
                         'FROM analytics a, analytics b, analytics c, analytics d').fetchall()
        assert error.value.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
    assert analytics.query('department_headcount', {'role': 'employee'})['rows']


def test_query_budget_and_connection_cleanup(database, monkeypatch):
    analytics = SQLAnalytics(database)
    analytics.STEP_BUDGET = 1
    opened = []
    original = analytics.open_readonly

    @contextmanager
    def capture():
        with original() as conn:
            opened.append(conn)
            yield conn

    monkeypatch.setattr(analytics, 'open_readonly', capture)
    with pytest.raises(sqlite3.OperationalError, match='interrupted'):
        analytics.query('department_headcount', {'role': 'admin'})
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        opened[0].execute('SELECT 1')
    analytics.STEP_BUDGET = 100_000
    assert analytics.query('department_headcount', {'role': 'admin'})['rows']
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        opened[1].execute('SELECT 1')


def test_output_scope_defense(database, monkeypatch):
    class UnexpectedResult:
        def execute(self, template, parameters):
            return self

        def fetchmany(self, limit):
            return [{'scope': 'finance', 'label': 'Secret', 'period': '2026-Q4', 'value': 1, 'unit': 'USD'}]

    @contextmanager
    def unexpected_connection():
        yield UnexpectedResult()

    analytics = SQLAnalytics(database)
    monkeypatch.setattr(analytics, 'open_readonly', unexpected_connection)
    with pytest.raises(PermissionError, match='outside authorized scopes'):
        analytics.query('department_headcount', {'role': 'employee'})


def test_unknown_scopes_excluded_even_for_admin(database):
    with database.connect() as conn:
        conn.execute('INSERT INTO analytics(metric_id,scope,label,period,value,unit) VALUES(?,?,?,?,?,?)',
                     ('department_headcount', 'unrecognized', 'Unknown scope', '2026-Q4', 1, 'people'))
    rows = SQLAnalytics(database).query('department_headcount', {'role': 'admin'})['rows']
    assert len(rows) == 5
    assert all(row['label'] != 'Unknown scope' for row in rows)
