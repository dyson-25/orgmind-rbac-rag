"""Bound, read-only analytics for an authenticated, current user from Auth.verify.

Only query() is an API entry point. open_readonly() is a trusted internal testing
hook: its SQL authorizer restricts data access, but does not enforce row scopes.
Each connection has a 100,000 VM-step budget; API results are capped at 50 rows.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.rbac_guard import ROLE_SCOPES, SCOPES, authorized_scopes


_METRIC_ROLES = {
    'department_headcount': frozenset(ROLE_SCOPES),
    'quarterly_budget_summary': frozenset(('admin', 'finance')),
    'sla_uptime_metrics': frozenset(('admin', 'engineering')),
    'compensation_band_summary': frozenset(('admin', 'hr')),
    'vendor_spend_rollup': frozenset(('admin', 'finance', 'hr')),
}
_COLUMNS = ('label', 'period', 'value', 'unit')
_READ_COLUMNS = frozenset((*_COLUMNS, 'metric_id', 'scope'))
_MAX_ROWS = 50
# The scope slots are fixed by the authoritative scope vocabulary, not input SQL.
_TEMPLATE = (
    'SELECT label, period, value, unit, scope FROM analytics '
    'WHERE metric_id = ? AND scope IN (' + ','.join('?' for _ in SCOPES) + ') '
    'AND (? IS NULL OR period = ?) '
    'ORDER BY label, period, scope, value, unit LIMIT ?'
)


class SQLAnalytics:
    STEP_BUDGET = 100_000
    PROGRESS_STEPS = 100

    def __init__(self, db):
        self.db = db

    @staticmethod
    def _authorize(action, table, column, database, source):
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if (action == sqlite3.SQLITE_READ and database == 'main'
                and table == 'analytics' and column in _READ_COLUMNS
                and source is None):
            return sqlite3.SQLITE_OK
        # Templates need no SQL functions; all other operations fail closed.
        return sqlite3.SQLITE_DENY

    @contextmanager
    def open_readonly(self):
        """Open a restricted connection, closing it on success or interruption."""
        connection = sqlite3.connect(
            Path(self.db.path).resolve().as_uri() + '?mode=ro',
            uri=True, isolation_level=None, cached_statements=0, timeout=5,
        )
        steps = 0
        interval = max(1, min(self.PROGRESS_STEPS, self.STEP_BUDGET))

        def progress():
            nonlocal steps
            steps += interval
            return int(steps >= self.STEP_BUDGET)

        try:
            connection.row_factory = sqlite3.Row
            connection.set_authorizer(self._authorize)
            connection.set_progress_handler(progress, interval)
            yield connection
        finally:
            connection.close()

    def query(self, metric_id, user, period=None):
        """Return metric_id/columns/rows/max_rows; never accept SQL or caller scopes.

        Unknown metrics raise ValueError; unauthorized roles raise PermissionError.
        SQLite execution-budget interruptions propagate as OperationalError.
        """
        if not isinstance(metric_id, str) or metric_id not in _METRIC_ROLES:
            raise ValueError('Unknown metric')
        role = user.get('role') if isinstance(user, dict) else None
        if not isinstance(role, str) or role not in _METRIC_ROLES[metric_id]:
            raise PermissionError('Metric not authorized for current role')
        if period is not None and not isinstance(period, str):
            raise ValueError('Period must be a string or None')
        scopes = tuple(authorized_scopes(user))
        parameters = (
            metric_id, *scopes, *([None] * (len(SCOPES) - len(scopes))),
            period, period, _MAX_ROWS,
        )
        with self.open_readonly() as connection:
            records = connection.execute(_TEMPLATE, parameters).fetchmany(_MAX_ROWS)
        if any(record['scope'] not in scopes for record in records):
            raise PermissionError('Analytics result outside authorized scopes')
        return {
            'metric_id': metric_id,
            'columns': list(_COLUMNS),
            'rows': [{column: record[column] for column in _COLUMNS} for record in records],
            'max_rows': _MAX_ROWS,
        }
