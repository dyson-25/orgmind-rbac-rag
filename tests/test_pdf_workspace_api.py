import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import Sessions, create_app

HEADERS = {'X-OrgMind-Request': '1'}


def test_visitors_are_isolated_on_every_document_and_conversation_route(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app, headers=HEADERS) as a:
        with TestClient(app, headers=HEADERS) as b:
            doc = a.post('/api/documents/sample').json()
            cid = a.post('/api/chat', json={'query': 'quantum chromodynamics'}).json()['conversation_id']
            assert b.get('/api/documents').json() == {'documents': []}
            assert a.cookies.get('orgmind_session') != b.cookies.get('orgmind_session')
            with app.state.db.connect() as conn:
                chunk = dict(conn.execute('SELECT * FROM chunks WHERE document_id=?', (doc['id'],)).fetchone())
            paths = [f"/api/documents/{doc['id']}/sources/{chunk['id']}", f"/api/documents/{doc['id']}/preview"]
            for path in paths:
                assert b.get(path).status_code == 404
                assert a.get(path).status_code == 200
            assert b.delete(f"/api/documents/{doc['id']}").status_code == 404
            assert b.post('/api/chat', json={'query': 'monthly income', 'document_ids': [doc['id']]}).status_code == 404
            assert b.post('/api/chat', json={'query': 'monthly income'}).json()['status'] == 'insufficient_evidence'
            assert b.post('/api/chat', json={'query': 'more', 'conversation_id': cid}).status_code == 404
            other = b.post('/api/documents/sample').json()
            assert other['id'] != doc['id']
            assert a.get('/api/documents').json()['documents'] == [doc]
            assert b.get('/api/documents').json()['documents'] == [other]
            assert b.delete(f"/api/documents/{other['id']}").status_code == 200
            assert a.post('/api/chat', json={'query': 'quantum chromodynamics', 'conversation_id': cid}).status_code == 200
            assert a.get(paths[1]).status_code == 200


def test_expired_or_forged_cookie_never_adopts_previous_workspace(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app, headers=HEADERS) as client:
        doc = client.post('/api/documents/sample').json()
        old_cookie = client.cookies.get('orgmind_session')
        with app.state.db.connect() as conn:
            conn.execute('UPDATE sessions SET expires_at=0')
        assert client.get('/api/documents').json() == {'documents': []}
        assert client.cookies.get('orgmind_session') != old_cookie
        assert client.get(f"/api/documents/{doc['id']}/preview").status_code == 404
        assert app.state.db.original_path(doc['id']).exists()
        client.cookies.clear()
        client.cookies.set('orgmind_session', 'forged')
        assert client.get('/api/documents').json() == {'documents': []}


@pytest.mark.parametrize('options', [
    {'upload_requests_per_minute': 1},
    {'ip_upload_requests_per_minute': 1},
])
def test_upload_limits_apply_to_sample_and_file_routes(tmp_path, options):
    with TestClient(create_app(Settings(data_dir=tmp_path, **options)), headers=HEADERS) as client:
        assert client.post('/api/documents/sample').status_code == 200
        response = client.post('/api/documents/upload', files={'file': ('bad.pdf', b'bad')})
        assert response.status_code == 429
        assert response.headers['retry-after'] == '60'


def test_global_generation_limit_is_independent_of_ip_and_session(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, requests_per_minute=120, global_requests_per_minute=1))
    with TestClient(app, headers=HEADERS, client=('198.51.100.1', 10)) as a:
        with TestClient(app, headers=HEADERS, client=('198.51.100.2', 20)) as b:
            assert a.post('/api/chat', json={'query': 'budget'}).status_code == 200
            assert b.post('/api/chat', json={'query': 'budget'}).status_code == 429


def test_generation_ip_limit_survives_cookie_rotation(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, requests_per_minute=1, global_requests_per_minute=120))
    with TestClient(app, headers=HEADERS) as client:
        assert client.post('/api/chat', json={'query': 'budget'}).status_code == 200
        client.cookies.clear()
        assert client.post('/api/chat', json={'query': 'budget'}).status_code == 429


def test_session_generation_limit_survives_ip_change(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, requests_per_minute=1, global_requests_per_minute=120))
    with TestClient(app, headers=HEADERS, client=('198.51.100.1', 10)) as a:
        with TestClient(app, headers=HEADERS, client=('198.51.100.2', 20)) as b:
            assert a.post('/api/chat', json={'query': 'budget'}).status_code == 200
            b.cookies.update(a.cookies)
            assert b.post('/api/chat', json={'query': 'budget'}).status_code == 429


def test_rate_cache_does_not_evict_active_limits(monkeypatch):
    limiter = Sessions()
    monkeypatch.setattr('app.main.time.monotonic', lambda: 100)
    for index in range(4096):
        limiter.limit(('client', index), 1)
    with pytest.raises(HTTPException) as full:
        limiter.limit(('new-client',), 1)
    assert full.value.status_code == 429
    with pytest.raises(HTTPException):
        limiter.limit(('client', 0), 1)
    monkeypatch.setattr('app.main.time.monotonic', lambda: 161)
    limiter.limit(('new-client',), 1)
    assert len(limiter.attempts) == 1
