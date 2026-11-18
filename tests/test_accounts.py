from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from app.accounts import register_accounts
from app.auth import Auth
from app.config import Settings
from app.database import AccountConflict, Database
from app.manage import set_password
from app.rbac_guard import ROLE_SCOPES

PASSWORD = 'account-test-password-only'


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    for key in ('DEMO_ADMIN_USERNAME', 'DEMO_ADMIN_PASSWORD', 'DEMO_EMPLOYEE_USERNAME', 'DEMO_EMPLOYEE_PASSWORD'):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(data_dir=tmp_path, jwt_secret='account-tests-only-' * 3, allow_demo=True)
    db = Database(settings)
    db.initialize()
    app = FastAPI()

    def bearer(authorization: str | None = Header(default=None)):
        if not authorization or not authorization.startswith('Bearer '):
            raise HTTPException(401, 'Authentication required')
        return authorization[7:]

    @contextmanager
    def identity(token, admin=False):
        with db.lock:
            user = app.state.auth.verify(token)
            if admin and user['role'] != 'admin':
                raise HTTPException(403, 'Administrator access required')
            yield user

    register_accounts(app, db, identity, bearer)
    app.state.auth = Auth(db, settings)
    with TestClient(app) as client:
        yield client, db, app.state.auth


def admin_headers(auth):
    return {'Authorization': 'Bearer ' + auth.demo('admin_user')['access_token']}


def create_user(client, auth, **changes):
    return client.post('/api/admin/users', headers=admin_headers(auth), json={
        'username': 'alex', 'name': 'Alex Example', 'email': 'Alex@Example.test', 'password': PASSWORD, **changes,
    })


def test_create_login_and_public_profile(accounts):
    client, db, auth = accounts
    response = create_user(client, auth, username='  ALEX  ')
    assert response.status_code == 201
    profile = response.json()
    assert profile['id'] == 'alex'
    assert profile['name'] == 'Alex Example'
    assert profile['email'] == 'alex@example.test'
    assert profile['role'] == 'employee' and profile['scopes'] == ['general']
    assert profile['active'] is True and profile['department'] is None
    assert db.user('alex')['password_hash'].startswith('$argon2id$')
    assert 'password' not in response.text and 'auth_version' not in response.text
    for login in ('ALEX', '  alex ', ' ALEX@EXAMPLE.TEST '):
        assert auth.verify(auth.login(login, PASSWORD)['access_token'])['id'] == 'alex'
    with pytest.raises(HTTPException) as error:
        auth.login('alex', 'incorrect')
    assert error.value.status_code == 401
    assert create_user(client, auth).status_code == 409
    assert create_user(client, auth, username='another', email='ALEX@EXAMPLE.TEST').status_code == 409
    assert create_user(client, auth, username='third', email='not-an-email').status_code == 422
    assert create_user(client, auth, username='third', role='superadmin').status_code == 422
    assert create_user(client, auth, username='third', department_id='missing').status_code == 422
    with db.connect() as conn:
        assert PASSWORD not in str([tuple(row) for row in conn.execute('SELECT * FROM audit_logs')])
    assert db.verify_audit()


@pytest.mark.parametrize('method,path,body', [
    ('get', '/api/admin/users', None), ('post', '/api/admin/users', {'username': 'alex', 'name': 'Alex', 'password': PASSWORD}),
    ('get', '/api/admin/users/eng_user', None), ('patch', '/api/admin/users/eng_user', {'role': 'admin'}),
    ('post', '/api/admin/users/eng_user/password', {'password': PASSWORD}),
    ('post', '/api/admin/users/eng_user/activate', None), ('post', '/api/admin/users/eng_user/deactivate', None),
    ('get', '/api/admin/roles', None), ('get', '/api/admin/departments', None),
    ('post', '/api/admin/departments', {'name': 'Finance'}), ('get', '/api/admin/departments/missing', None),
    ('patch', '/api/admin/departments/missing', {'name': 'Finance'}), ('delete', '/api/admin/departments/missing', None),
    ('get', '/api/admin/overview', None),
])
def test_every_account_endpoint_requires_active_admin(accounts, method, path, body):
    client, db, auth = accounts
    assert client.request(method, path, json=body).status_code == 401
    employee = auth.demo('general_user')['access_token']
    assert client.request(method, path, json=body, headers={'Authorization': 'Bearer ' + employee}).status_code == 403
    admin = admin_headers(auth)
    db.update_account('admin_user', {'name': 'Updated administrator'})
    assert client.request(method, path, json=body, headers=admin).status_code == 401


def test_account_changes_revoke_tokens(accounts):
    client, db, auth = accounts
    assert create_user(client, auth).status_code == 201
    header = admin_headers(auth)
    for changes in ({'name': 'New Name'}, {'email': 'new@example.test'}, {'role': 'finance'}, {'department_id': None}):
        token = auth.login('alex', PASSWORD)['access_token']
        version = db.user('alex')['auth_version']
        assert client.patch('/api/admin/users/alex', json=changes, headers=header).status_code == 200
        assert db.user('alex')['auth_version'] == version + 1
        with pytest.raises(HTTPException):
            auth.verify(token)
    old = auth.login('alex', PASSWORD)['access_token']
    assert client.post('/api/admin/users/alex/password', json={'password': 'changed-test-password'}, headers=header).status_code == 200
    with pytest.raises(HTTPException):
        auth.verify(old)
    with pytest.raises(HTTPException):
        auth.login('alex', PASSWORD)
    token = auth.login('alex', 'changed-test-password')['access_token']
    assert client.post('/api/admin/users/alex/deactivate', headers=header).json()['active'] is False
    with pytest.raises(HTTPException):
        auth.verify(token)
    with pytest.raises(HTTPException):
        auth.login('alex', 'changed-test-password')
    assert client.post('/api/admin/users/alex/activate', headers=header).json()['active'] is True
    with pytest.raises(HTTPException):
        auth.verify(token)
    assert auth.login('alex', 'changed-test-password')['user']['active'] is True
    set_password(db, ' NEW@EXAMPLE.TEST ', PASSWORD)
    assert auth.login('alex', PASSWORD)['user']['id'] == 'alex'


def test_direct_inactive_reauthorization_and_demo(accounts):
    _, db, auth = accounts
    token = auth.demo('eng_user')['access_token']
    with db.connect() as conn:
        conn.execute("UPDATE users SET active=0 WHERE id='eng_user'")
    for action in (lambda: auth.verify(token), lambda: auth.demo('eng_user'), lambda: auth.issue(db.user('eng_user'))):
        with pytest.raises(HTTPException) as error:
            action()
        assert error.value.status_code == 401


def test_last_active_admin_and_validation(accounts):
    client, db, auth = accounts
    header = admin_headers(auth)
    version, epoch = db.user('admin_user')['auth_version'], db.epoch()
    for changes in ({'role': 'employee'}, {'active': False}, {'active': False, 'role': 'employee'}):
        assert client.patch('/api/admin/users/admin_user', headers=header, json=changes).status_code == 409
    assert client.post('/api/admin/users/admin_user/deactivate', headers=header).status_code == 409
    assert db.epoch() == epoch and db.user('admin_user')['auth_version'] == version
    assert create_user(client, auth, role='admin', active=False).status_code == 201
    assert client.post('/api/admin/users/admin_user/deactivate', headers=header).status_code == 409
    assert client.post('/api/admin/users/alex/activate', headers=header).status_code == 200
    assert client.patch('/api/admin/users/admin_user', headers=header, json={'role': 'employee'}).status_code == 200
    assert client.get('/api/admin/users', headers=header).status_code == 401
    new_header = {'Authorization': 'Bearer ' + auth.login('alex', PASSWORD)['access_token']}
    assert client.post('/api/admin/users/alex/deactivate', headers=new_header).status_code == 409
    assert client.patch('/api/admin/users/alex', headers=new_header, json={'active': None}).status_code == 422
    assert client.patch('/api/admin/users/alex', headers=new_header, json={'password': None}).status_code == 422
    assert client.patch('/api/admin/users/alex', headers=new_header, json={}).status_code == 422
    assert client.patch('/api/admin/users/alex', headers=new_header, json={'scopes': ['executive']}).status_code == 422
    assert client.get('/api/admin/users/missing', headers=new_header).status_code == 404
    assert client.patch('/api/admin/users/missing', headers=new_header, json={'name': 'New'}).status_code == 404


def test_department_crud_descriptive_assignment_and_counts(accounts):
    client, db, auth = accounts
    header = admin_headers(auth)
    result = client.post('/api/admin/departments', headers=header, json={'name': 'Finance', 'description': 'Planning'})
    assert result.status_code == 201
    department = result.json()
    did = department['id']
    assert create_user(client, auth, department_id=did).status_code == 201
    token = auth.login('alex', PASSWORD)['access_token']
    assert auth.public(auth.verify(token))['scopes'] == ('general',)
    assert auth.public(auth.verify(token))['department']['name'] == 'Finance'
    assert client.get('/api/admin/departments', headers=header).json()['departments'][0]['user_count'] == 1
    assert client.post('/api/admin/departments', headers=header, json={'name': ' finance '}).status_code == 409
    assert client.patch(f'/api/admin/departments/{did}', headers=header, json={'name': 'Accounting'}).status_code == 200
    with pytest.raises(HTTPException):
        auth.verify(token)
    assert client.get(f'/api/admin/departments/{did}', headers=header).json()['name'] == 'Accounting'
    assert client.get('/api/admin/users/alex', headers=header).json()['department']['name'] == 'Accounting'
    overview = client.get('/api/admin/overview', headers=header).json()
    assert overview['users'] == 7 and overview['active_users'] == 7 and overview['active_admins'] == 1
    assert overview['departments'] == 1 and overview['documents'] == 0 and overview['chunks'] == 0
    roles = client.get('/api/admin/roles', headers=header).json()['roles']
    assert {role['id'] for role in roles} == set(ROLE_SCOPES)
    assert sum(role['user_count'] for role in roles) == 7
    fresh = auth.login('alex', PASSWORD)['access_token']
    assert client.delete(f'/api/admin/departments/{did}', headers=header).status_code == 204
    assert db.user('alex')['department_id'] is None
    with pytest.raises(HTTPException):
        auth.verify(fresh)
    assert client.delete(f'/api/admin/departments/{did}', headers=header).status_code == 404
    with db.connect() as conn:
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    assert db.verify_audit()


def test_last_admin_check_serializes_independent_database_instances(accounts):
    _, db, _ = accounts
    db.create_account('second_admin', name='Second Admin', password=PASSWORD, role='admin')
    other = Database(db.settings)

    def deactivate(database, uid):
        try:
            database.update_account(uid, {'active': False})
            return 'updated'
        except AccountConflict:
            return 'protected'

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(deactivate, db, 'admin_user')
        second = pool.submit(deactivate, other, 'second_admin')
        assert sorted([first.result(), second.result()]) == ['protected', 'updated']
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM users WHERE role='admin' AND active=1").fetchone()[0] == 1


def test_department_document_deletion_is_not_implicit_visibility_change(accounts):
    client, db, auth = accounts
    header = admin_headers(auth)
    did = client.post('/api/admin/departments', headers=header, json={'name': 'Engineering'}).json()['id']
    with db.connect() as conn:
        conn.execute('INSERT INTO documents(id,title,filename,pages,metadata,digest,department_id,visibility) '
                     'VALUES(?,?,?,?,?,?,?,?)', ('upload', 'Test', 'test.pdf', 1, '{}', 'digest', did, 'department'))
    assert client.delete(f'/api/admin/departments/{did}', headers=header).status_code == 409
    with db.connect() as conn:
        assert conn.execute("SELECT department_id FROM documents WHERE id='upload'").fetchone()[0] == did
