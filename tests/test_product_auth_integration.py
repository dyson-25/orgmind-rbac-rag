from app.config import Settings
from tests.test_orgmind_security import client, headers


def test_production_disables_passwordless_personas_by_default(monkeypatch):
    monkeypatch.delenv('ALLOW_DEMO_PERSONA', raising=False)
    monkeypatch.setenv('APP_ENV', 'production')
    assert Settings(jwt_secret='test-only-secret-' * 3).allow_demo is False


def test_each_login_gets_an_independent_session(client):
    first = client.post('/api/auth/demo-persona', json={'persona_id': 'general_user'}).json()
    second = client.post('/api/auth/demo-persona', json={'persona_id': 'general_user'}).json()
    assert first['access_token'] != second['access_token']
    for result in (first, second):
        assert client.get('/api/auth/me', headers={'Authorization': f"Bearer {result['access_token']}"}).status_code == 200


def test_accounts_are_registered_and_support_long_email_login(client):
    email = 'employee.' + 'a' * 90 + '@example.test'
    created = client.post('/api/admin/users', headers=headers(client), json={
        'username': 'long_email', 'name': 'Long Email', 'email': email,
        'password': 'synthetic-test-password', 'role': 'employee',
    })
    assert created.status_code == 201
    signed_in = client.post('/api/auth/login', json={'username': email.upper(), 'password': 'synthetic-test-password'})
    assert signed_in.status_code == 200
    assert signed_in.json()['user']['id'] == 'long_email'


def test_legacy_role_endpoint_protects_last_admin(client):
    result = client.patch('/api/admin/users/admin_user/role', headers=headers(client), json={'role': 'employee'})
    assert result.status_code == 409
    assert client.app.state.db.user('admin_user')['role'] == 'admin'
