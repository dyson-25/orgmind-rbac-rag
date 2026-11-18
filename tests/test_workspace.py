import json
from contextlib import contextmanager
from pathlib import Path

import fitz
import pytest
from fastapi import Header, HTTPException
from fastapi.testclient import TestClient

from app.config import Settings
from app.llm_service import LLMService
from app.main import create_app
from app.rbac_guard import authorized_chunk_ids, authorized_document_ids
from app.workspace import index_original, recover_workspace, register_workspace


@pytest.fixture(scope='module')
def workspace(tmp_path_factory):
    settings = Settings(data_dir=tmp_path_factory.mktemp('workspace'), jwt_secret='workspace-test-key-' * 4, allow_demo=True)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('GENERATION_PROVIDER', 'local')
        app = create_app(settings, llm=LLMService())
    # Permit focused execution before the parent wires register_workspace into main.
    if not any(route.path == '/api/workspace/chat' for route in app.routes):
        def bearer(authorization: str | None = Header(default=None)):
            if not authorization or not authorization.startswith('Bearer '):
                raise HTTPException(401)
            return authorization[7:]

        @contextmanager
        def identity(token, admin=False):
            with app.state.db.lock:
                user = app.state.auth.verify(token)
                if admin and user['role'] != 'admin':
                    raise HTTPException(403)
                yield user

        register_workspace(app, app.state.db, app.state.rag, identity, bearer)
    with TestClient(app) as client:
        yield client


def headers(client, uid='general_user'):
    response = client.post('/api/auth/demo-persona', json={'persona_id': uid})
    assert response.status_code == 200
    return {'Authorization': 'Bearer ' + response.json()['access_token']}


def pdf_bytes(text='Synthetic Zephyr picnic transport reimbursement is 42 credits. Teams submit receipts on Friday.'):
    with fitz.open() as pdf:
        pdf.new_page().insert_text((50, 50), text)
        return pdf.tobytes()


def upload(client, header, raw=None, filename='synthetic.pdf'):
    response = client.post('/api/workspace/documents/upload', headers=header,
                           files={'file': (filename, raw if raw is not None else pdf_bytes(), 'application/pdf')})
    assert response.status_code == 201, response.text
    return response.json()


def test_private_upload_duplicate_ownership_and_filtering(workspace):
    client = workspace
    owner, other, admin = headers(client), headers(client, 'eng_user'), headers(client, 'admin_user')
    raw = pdf_bytes()
    first, duplicate = upload(client, owner, raw), upload(client, other, raw)
    assert first['id'] != duplicate['id']
    assert first['status'] == 'indexed' and first['chunks'] > 0
    assert first['visibility'] == 'private' and first['owner_id'] == 'general_user'
    assert first['can_delete'] and first['can_reindex'] and first['has_original']
    assert first['allowed_scopes'] == []
    rows = client.get('/api/workspace/documents?q=synthetic&status=indexed', headers=owner).json()['documents']
    assert first['id'] in {row['id'] for row in rows}
    assert duplicate['id'] not in {row['id'] for row in rows}
    assert 'DOC_09' not in {row['id'] for row in client.get('/api/workspace/documents', headers=admin).json()['documents']}
    for endpoint in ('download', 'preview', 'reindex'):
        response = getattr(client, 'post' if endpoint == 'reindex' else 'get')(
            f"/api/workspace/documents/{first['id']}/{endpoint}", headers=other)
        assert response.status_code == 404
    assert client.delete(f"/api/workspace/documents/{first['id']}", headers=other).status_code == 404
    assert client.get(f"/api/workspace/documents/{first['id']}/preview?page=1", headers=owner).content.startswith(b'\x89PNG')
    assert client.get(f"/api/workspace/documents/{first['id']}/preview?page=2", headers=owner).status_code == 404
    assert client.get(f"/api/workspace/documents/{first['id']}/download", headers=owner).content == raw
    assert client.get('/api/workspace/documents/DOC_01/preview', headers=owner).status_code == 200
    db = client.app.state.db
    with db.connect() as conn:
        private = {r['id'] for r in conn.execute('SELECT id FROM chunks WHERE document_id=?', (first['id'],))}
        assert private <= authorized_chunk_ids(conn, db.user('general_user'))
        assert not private & authorized_chunk_ids(conn, db.user('eng_user'))
        assert first['id'] not in authorized_document_ids(conn, db.user('eng_user'))
    assert not {c['id'] for c in client.app.state.rag.retrieve('Zephyr picnic', db.user('eng_user'))} & private
    assert client.get('/api/citations/' + next(iter(private)), headers=other).status_code == 404


def test_permissions_private_overrides_scopes_and_revoke_conversation(workspace):
    client = workspace
    owner, other, admin = headers(client), headers(client, 'eng_user'), headers(client, 'admin_user')
    doc = upload(client, owner)
    url = f"/api/workspace/documents/{doc['id']}/permissions"
    body = {'visibility': 'scoped', 'allowed_scopes': ['general']}
    assert client.patch(url, headers=owner, json=body).status_code == 403
    shared = client.patch(url, headers=admin, json=body).json()
    assert shared['owner_id'] == 'general_user'
    response = client.post('/api/workspace/chat', headers=other,
                           json={'query': 'Summarize the picnic policy', 'document_ids': [doc['id']]})
    assert response.status_code == 200, response.text
    cid = response.json()['conversation_id']
    assert response.json()['citations']
    assert client.get('/api/workspace/conversations/' + cid, headers=other).status_code == 200
    assert client.patch(url, headers=admin, json={'visibility': 'private', 'allowed_scopes': ['general']}).status_code == 200
    assert client.get('/api/workspace/conversations/' + cid, headers=other).status_code == 404
    assert client.post('/api/workspace/chat', headers=other, json={'query': 'Explain more', 'conversation_id': cid}).status_code == 404
    assert client.post('/api/workspace/chat', headers=other, json={'query': 'Explain more', 'document_ids': [doc['id']]}).status_code == 404
    assert client.get(f"/api/workspace/documents/{doc['id']}/download", headers=other).status_code == 404


def test_failed_row_recovery_reindex_delete_and_fts(workspace, monkeypatch):
    client = workspace
    owner = headers(client)
    doc = upload(client, owner, b'%PDF-broken')
    assert doc['status'] == 'failed' and doc['chunks'] == 0 and doc['has_original']
    db, rag = client.app.state.db, client.app.state.rag
    with db.connect() as conn:
        row = dict(conn.execute('SELECT * FROM documents WHERE id=?', (doc['id'],)).fetchone())
        path = Path(row['storage_path'])
        path.write_bytes(pdf_bytes())
        conn.execute("UPDATE documents SET status='processing' WHERE id=?", (doc['id'],))
    recover_workspace(db, rag)
    assert client.post(f"/api/workspace/documents/{doc['id']}/reindex", headers=owner).json()['status'] == 'indexed'
    with db.connect() as conn:
        chunks = {r[0] for r in conn.execute('SELECT id FROM chunks WHERE document_id=?', (doc['id'],))}
    original = rag._insert_chunks

    def partial(conn, doc_id, pages):
        original(conn, doc_id, pages)
        raise ValueError('synthetic indexing failure')

    with monkeypatch.context() as scoped:
        scoped.setattr(rag, '_insert_chunks', partial)
        assert not index_original(db, rag, doc['id'])
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM chunks WHERE document_id=?', (doc['id'],)).fetchone()[0] == 0
        assert not chunks & {r[0] for r in conn.execute('SELECT chunk_id FROM chunks_fts')}
    assert client.post(f"/api/workspace/documents/{doc['id']}/reindex", headers=owner).json()['status'] == 'indexed'
    assert client.delete(f"/api/workspace/documents/{doc['id']}", headers=owner).status_code == 204
    assert not path.exists()
    with db.connect() as conn:
        assert not conn.execute('SELECT 1 FROM documents WHERE id=?', (doc['id'],)).fetchone()
        assert not chunks & {r[0] for r in conn.execute('SELECT chunk_id FROM chunks_fts')}


def test_greeting_empty_selection_followup_and_session_scope(workspace):
    client = workspace
    owner, other = headers(client), headers(client, 'eng_user')
    doc = upload(client, owner)
    greeting = client.post('/api/workspace/chat', headers=owner, json={'query': 'Hello'}).json()
    assert greeting['answer'].startswith('Hello') and not greeting['citations']
    empty = client.post('/api/workspace/chat', headers=owner, json={'query': 'What is the picnic rate?', 'document_ids': []}).json()
    assert not empty['citations']
    first = client.post('/api/workspace/chat', headers=owner,
                        json={'query': 'What is the Zephyr picnic reimbursement?', 'document_ids': [doc['id']]}).json()
    assert first['citations'] and '42 credits' in first['answer']
    second = client.post('/api/workspace/chat', headers=owner,
                         json={'query': 'Explain more', 'conversation_id': first['conversation_id']}).json()
    assert second['citations'] and second['conversation_id'] == first['conversation_id']
    assert {c['document_id'] for c in second['citations']} == {doc['id']}
    url = '/api/workspace/conversations/' + first['conversation_id']
    assert client.get(url, headers=other).status_code == 404
    assert client.get(url, headers=owner).json()['messages'][0]['content'] == 'What is the Zephyr picnic reimbursement?'
    assert client.delete(url, headers=owner).status_code == 204
    assert client.get(url, headers=owner).status_code == 404
    assert client.post('/api/workspace/chat', headers=owner, json={'query': 'Summarize', 'document_ids': ['DOC_09']}).status_code == 404


class ProductAdapter:
    provider = 'gemini'

    def __init__(self, kind='success'):
        self.kind = kind
        self.messages = None
        self.last_diagnostics = {'provider': 'gemini', 'model': 'synthetic-model', 'http_status': 200, 'error_category': None}

    def generate(self, messages):
        self.messages = messages
        if self.kind == 'failure':
            self.last_diagnostics.update(http_status=503, error_category='service')
            raise RuntimeError('never surface synthetic secrets')
        chunks = json.loads(messages[1]['content'])['authorized_document_context']
        return json.dumps({'answer': 'The picnic reimbursement is 42 credits. Teams submit receipts on Friday.',
                           'citation_ids': ['invented'] if self.kind == 'forged' else [chunks[0]['id']]})


@pytest.mark.parametrize('kind', ['success', 'failure', 'forged'])
def test_mocked_provider_and_grounded_citations(workspace, monkeypatch, kind):
    client = workspace
    owner = headers(client)
    doc = upload(client, owner)
    adapter = ProductAdapter(kind)
    monkeypatch.setitem(client.app.state._state, 'llm', LLMService(adapter=adapter))
    response = client.post('/api/workspace/chat', headers=owner,
                           json={'query': 'Summarize the picnic reimbursement', 'document_ids': [doc['id']]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['provider_status']['succeeded'] == (kind == 'success')
    assert body['provider_status']['fallback_used'] == (kind != 'success')
    assert body['citations'] and {c['document_id'] for c in body['citations']} == {doc['id']}
    context = json.loads(adapter.messages[1]['content'])['authorized_document_context']
    assert {c['document_id'] for c in context} == {doc['id']}
    assert all(m['role'] != 'assistant' for m in adapter.messages)
    assert 'never surface' not in response.text
    db = client.app.state.db
    with db.connect() as conn:
        audit = conn.execute("SELECT decision FROM audit_logs WHERE action='provider' ORDER BY id DESC LIMIT 1").fetchone()
        assert audit[0] == ('success' if kind == 'success' else 'fallback')
    assert db.verify_audit()


def test_limits_and_unauthenticated_workspace(workspace):
    client = workspace
    owner = headers(client)
    assert client.get('/api/workspace/documents').status_code == 401
    assert client.post('/api/workspace/chat', headers=owner, json={'query': ' '}).status_code == 422
    assert client.post('/api/workspace/chat', headers=owner, json={'query': 'q' * 2001}).status_code == 422
    assert client.post('/api/workspace/chat', headers=owner, content=b'x' * 17000).status_code == 413
    assert client.post('/api/workspace/documents/upload', headers=owner,
                       files={'file': ('bad.txt', b'not PDF', 'text/plain')}).status_code == 415
    assert client.post('/api/workspace/documents/upload', headers=owner, data={'owner_id': 'admin_user'},
                       files={'file': ('safe.pdf', pdf_bytes(), 'application/pdf')}).status_code == 413
    assert client.get('/api/workspace/documents?status=invented', headers=owner).status_code == 422
