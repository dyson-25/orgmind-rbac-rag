"""Administrator account management; department labels do not grant scopes."""
import secrets
import sqlite3
from contextlib import contextmanager

from fastapi import Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.database import AccountConflict
from app.rbac_guard import ROLE_SCOPES


class AccountModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class AccountCreate(AccountModel):
    username: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=12, max_length=256, repr=False)
    email: str | None = Field(default=None, max_length=254)
    role: str = 'employee'
    department_id: str | None = None
    active: StrictBool = True


class AccountUpdate(AccountModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=254)
    role: str | None = None
    department_id: str | None = None
    active: StrictBool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=256, repr=False)


class PasswordUpdate(AccountModel):
    password: str = Field(min_length=12, max_length=256, repr=False)


class DepartmentCreate(AccountModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=2000)


class DepartmentUpdate(AccountModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


@contextmanager
def account_errors():
    try:
        yield
    except KeyError:
        raise HTTPException(404, 'Account or department not found') from None
    except AccountConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except sqlite3.IntegrityError:
        raise HTTPException(409, 'Account or department already exists') from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


def register_accounts(app, db, identity, bearer):
    """identity(token, admin=True) must hold db.lock and reauthorize the token."""
    def public(user):
        return app.state.auth.public(user)

    def update(uid, changes, actor, action='user-update'):
        user = db.update_account(uid, changes)
        db.audit(actor, action, 'allow', rule='admin-account-management')
        return public(user)

    @app.get('/api/admin/users')
    def users(token=Depends(bearer)):
        with identity(token, admin=True), db.connect() as conn:
            rows = conn.execute('SELECT * FROM users ORDER BY id').fetchall()
            return {'users': [public(dict(row)) for row in rows]}

    @app.post('/api/admin/users', status_code=201)
    def create_user(body: AccountCreate, token=Depends(bearer)):
        with identity(token, admin=True) as actor, account_errors():
            user = db.create_account(**body.model_dump())
            db.audit(actor, 'user-create', 'allow', rule='admin-account-management')
            return public(user)

    @app.get('/api/admin/users/{uid}')
    def read_user(uid: str, token=Depends(bearer)):
        with identity(token, admin=True):
            user = db.user(uid)
            if not user:
                raise HTTPException(404, 'User not found')
            return public(user)

    @app.patch('/api/admin/users/{uid}')
    def edit_user(uid: str, body: AccountUpdate, token=Depends(bearer)):
        with identity(token, admin=True) as actor, account_errors():
            return update(uid, body.model_dump(exclude_unset=True), actor)

    @app.post('/api/admin/users/{uid}/password')
    def password(uid: str, body: PasswordUpdate, token=Depends(bearer)):
        with identity(token, admin=True) as actor, account_errors():
            return update(uid, body.model_dump(), actor, 'user-password')

    @app.post('/api/admin/users/{uid}/activate')
    def activate(uid: str, token=Depends(bearer)):
        with identity(token, admin=True) as actor, account_errors():
            return update(uid, {'active': True}, actor, 'user-activate')

    @app.post('/api/admin/users/{uid}/deactivate')
    def deactivate(uid: str, token=Depends(bearer)):
        with identity(token, admin=True) as actor, account_errors():
            return update(uid, {'active': False}, actor, 'user-deactivate')

    @app.get('/api/admin/roles')
    def roles(token=Depends(bearer)):
        with identity(token, admin=True), db.connect() as conn:
            counts = dict(conn.execute('SELECT role,count(*) FROM users GROUP BY role').fetchall())
            return {'roles': [{'id': role, 'name': role.title(), 'scopes': list(scopes),
                               'user_count': counts.get(role, 0)} for role, scopes in ROLE_SCOPES.items()]}

    @app.get('/api/admin/departments')
    def departments(token=Depends(bearer)):
        with identity(token, admin=True), db.connect() as conn:
            return {'departments': [dict(row) for row in conn.execute(
                'SELECT d.*, (SELECT count(*) FROM users u WHERE u.department_id=d.id) user_count '
                'FROM departments d ORDER BY d.name'
            )]}

    @app.post('/api/admin/departments', status_code=201)
    def create_department(body: DepartmentCreate, token=Depends(bearer)):
        with identity(token, admin=True) as actor, account_errors():
            name = body.name.strip()
            if not name:
                raise ValueError('Department name cannot be blank')
            department = {'id': secrets.token_hex(12), 'name': name, 'description': body.description.strip()}
            with db.connect() as conn:
                conn.execute('INSERT INTO departments(id,name,description) VALUES(?,?,?)', tuple(department.values()))
                db.bump_epoch(conn)
            db.audit(actor, 'department-create', 'allow', rule='admin-account-management')
            return department

    @app.get('/api/admin/departments/{department_id}')
    def read_department(department_id: str, token=Depends(bearer)):
        with identity(token, admin=True), db.connect() as conn:
            row = conn.execute('SELECT * FROM departments WHERE id=?', (department_id,)).fetchone()
            if not row:
                raise HTTPException(404, 'Department not found')
            return dict(row)

    @app.patch('/api/admin/departments/{department_id}')
    def edit_department(department_id: str, body: DepartmentUpdate, token=Depends(bearer)):
        with identity(token, admin=True) as actor, account_errors():
            values = body.model_dump(exclude_unset=True)
            if not values or any(value is None for value in values.values()):
                raise ValueError('Supply a department name or description')
            values = {key: value.strip() for key, value in values.items()}
            if 'name' in values and not values['name']:
                raise ValueError('Department name cannot be blank')
            with db.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                if not conn.execute('SELECT 1 FROM departments WHERE id=?', (department_id,)).fetchone():
                    raise KeyError('Department not found')
                conn.execute(f'UPDATE departments SET {",".join(f"{key}=?" for key in values)} WHERE id=?',
                             (*values.values(), department_id))
                conn.execute('UPDATE users SET auth_version=auth_version+1 WHERE department_id=?', (department_id,))
                db.bump_epoch(conn)
                result = dict(conn.execute('SELECT * FROM departments WHERE id=?', (department_id,)).fetchone())
            db.audit(actor, 'department-update', 'allow', rule='admin-account-management')
            return result

    @app.delete('/api/admin/departments/{department_id}', status_code=204)
    def delete_department(department_id: str, token=Depends(bearer)):
        with identity(token, admin=True) as actor, account_errors():
            db.delete_department(department_id)
            db.audit(actor, 'department-delete', 'allow', rule='admin-account-management')
            return Response(status_code=204)

    @app.get('/api/admin/overview')
    def overview(token=Depends(bearer)):
        with identity(token, admin=True), db.connect() as conn:
            counts = {table: conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                      for table in ('users', 'departments', 'documents', 'chunks', 'audit_logs')}
            counts['active_users'] = conn.execute('SELECT count(*) FROM users WHERE active=1').fetchone()[0]
            counts['inactive_users'] = counts['users'] - counts['active_users']
            counts['active_admins'] = conn.execute("SELECT count(*) FROM users WHERE active=1 AND role='admin'").fetchone()[0]
            return {**counts, 'acl_epoch': db.epoch(conn)}
