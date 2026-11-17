from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import HTTPException
from pwdlib import PasswordHash

from app.auth import Auth, ISSUER, AUDIENCE
from app.config import Settings
from app.database import Database
from app.rbac_guard import PERSONAS, ROLE_SCOPES


@pytest.fixture
def identity(tmp_path):
    settings = Settings(data_dir=tmp_path, jwt_secret='test-only-' * 5)
    db = Database(settings)
    db.initialize()
    return db, Auth(db, settings), settings


def test_seed_idempotent(identity):
    db, _, _ = identity
    db.initialize()
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM users').fetchone()[0] == 6
        assert conn.execute('SELECT count(*) FROM analytics').fetchone()[0] == 14
    assert {db.user(uid)['role'] for uid in PERSONAS} == set(ROLE_SCOPES)


def test_passwords_argon2id(identity):
    db, _, _ = identity
    assert all(db.user(uid)['password_hash'].startswith('$argon2id$') for uid in PERSONAS)


def test_local_password_provisioning(identity):
    from app.manage import set_password
    db, auth, _ = identity
    token = auth.demo('eng_user')['access_token']
    set_password(db, 'eng_user', 'synthetic-test-password')
    assert auth.login('eng_user', 'synthetic-test-password')['user']['id'] == 'eng_user'
    with pytest.raises(HTTPException): auth.verify(token)
    with pytest.raises(ValueError): set_password(db, 'eng_user', 'short')
    with db.connect() as conn:
        assert 'synthetic-test-password' not in str([tuple(r) for r in conn.execute('SELECT * FROM audit_logs')])


def test_real_login(identity):
    db, auth, _ = identity
    with db.connect() as conn:
        conn.execute('UPDATE users SET password_hash=? WHERE id=?', (PasswordHash.recommended().hash('test-password'), 'eng_user'))
    assert auth.verify(auth.login('eng_user', 'test-password')['access_token'])['role'] == 'engineering'
    with pytest.raises(HTTPException):
        auth.login('eng_user', 'incorrect')


@pytest.mark.parametrize('change', ['signature', 'expired', 'algorithm', 'issuer', 'audience', 'role'])
def test_bad_jwt_rejected(identity, change):
    db, auth, settings = identity
    payload = {'sub': 'eng_user', 'role': 'engineering', 'ver': 0, 'iss': ISSUER, 'aud': AUDIENCE,
               'exp': datetime.now(timezone.utc) + timedelta(hours=1), 'iat': datetime.now(timezone.utc)}
    key, algorithm = settings.jwt_secret, 'HS256'
    if change == 'signature': key = 'another-key' * 5
    if change == 'expired': payload['exp'] = datetime.now(timezone.utc) - timedelta(hours=1)
    if change == 'algorithm': algorithm = 'HS384'
    if change == 'issuer': payload['iss'] = 'other'
    if change == 'audience': payload['aud'] = 'other'
    if change == 'role': payload['role'] = 'admin'
    with pytest.raises(HTTPException) as exc:
        auth.verify(jwt.encode(payload, key, algorithm=algorithm))
    assert exc.value.status_code == 401


def test_role_downgrade(identity):
    db, auth, _ = identity
    token = auth.demo('finance_user')['access_token']
    db.change_role('finance_user', 'employee')
    assert db.epoch() == 1
    with pytest.raises(HTTPException): auth.verify(token)


def test_pytest_uses_explicit_test_key(tmp_path):
    settings = Settings(data_dir=tmp_path)
    assert settings.jwt_secret == 'orgmind-pytest-only-key-not-for-deployment-2026'


def test_development_missing_key_still_warns(tmp_path):
    with pytest.warns(UserWarning, match='ephemeral development-only JWT secret'):
        settings = Settings(data_dir=tmp_path, environment='development', jwt_secret='')
    assert len(settings.jwt_secret) >= 32


def test_production_requires_key(tmp_path):
    with pytest.raises(ValueError): Settings(data_dir=tmp_path, environment='production', jwt_secret='')


def test_production_demo_disabled(identity):
    db, _, settings = identity
    settings.environment = 'production'
    settings.allow_demo = False
    with pytest.raises(HTTPException) as exc: Auth(db, settings).demo('admin_user')
    assert exc.value.status_code == 403


def test_arbitrary_persona_rejected(identity):
    with pytest.raises(HTTPException): identity[1].demo('super_admin')


@pytest.mark.parametrize('persona,expected', [
    ('admin_user', {'general', 'hr', 'finance', 'engineering', 'executive'}),
    ('finance_user', {'general', 'finance'}), ('hr_user', {'general', 'hr'}),
    ('eng_user', {'general', 'engineering'}), ('general_user', {'general'}),
    ('executive_user', {'general', 'executive'}),
])
def test_role_scope_authority(identity, persona, expected):
    db, auth, _ = identity
    user = auth.verify(auth.demo(persona)['access_token'])
    assert set(auth.public(user)['scopes']) == expected


def test_audit_survives_restart(identity):
    db, auth, settings = identity
    auth.demo('admin_user')
    restarted = Database(settings)
    restarted.initialize()
    assert restarted.verify_audit()


def test_document_seed_does_not_restore_deleted_metadata(identity, tmp_path):
    from app.pdf_corpus import generate_corpus
    db, _, _ = identity
    specs = generate_corpus(tmp_path / 'corpus')
    db.seed_documents(specs)
    with db.connect() as conn:
        conn.execute("DELETE FROM documents WHERE id='DOC_01'")
    db.seed_documents(specs)
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 8


def test_missing_user_revokes_token(identity):
    db, auth, _ = identity
    token = auth.demo('eng_user')['access_token']
    with db.connect() as conn: conn.execute("DELETE FROM users WHERE id='eng_user'")
    with pytest.raises(HTTPException): auth.verify(token)


def test_corpus_metadata_seed(identity, tmp_path):
    from app.pdf_corpus import generate_corpus
    db, _, _ = identity
    specs = generate_corpus(tmp_path / 'corpus')
    db.seed_documents(specs)
    db.seed_documents(specs)
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 9
        assert conn.execute('SELECT min(pages) FROM documents').fetchone()[0] >= 2
    assert db.epoch() == 1


def test_audit_integrity(identity):
    db, auth, _ = identity
    auth.demo('admin_user')
    auth.demo('eng_user')
    assert db.verify_audit()
    with db.connect() as conn: conn.execute("UPDATE audit_logs SET decision='deny' WHERE id=1")
    assert not db.verify_audit()
