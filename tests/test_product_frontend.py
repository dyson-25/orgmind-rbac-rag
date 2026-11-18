"""Product frontend contracts; original studio tests remain unchanged."""
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from tests.test_orgmind_security import client, headers, isolated  # noqa: F401


STATIC = Path(__file__).resolve().parents[1] / 'static'


class ProductMarkup(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.elements = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    @property
    def ids(self):
        return {attrs['id']: attrs for _, attrs in self.elements if 'id' in attrs}


@pytest.fixture(scope='module')
def markup():
    return ProductMarkup((STATIC / 'index.html').read_text())


@pytest.fixture(scope='module')
def javascript():
    return (STATIC / 'app.js').read_text()


def test_public_landing_and_real_credential_form(client, markup, javascript):
    response = client.get('/')
    assert response.status_code == 200
    assert 'Explore Demo' in response.text and 'Sign In' in response.text
    assert 'synthetic' in response.text.lower()
    assert 'hidden' in markup.ids['app-shell']
    assert 'hidden' not in markup.ids['landing']
    assert markup.ids['password']['type'] == 'password'
    assert markup.ids['username']['autocomplete'] == 'username'
    assert 'value' not in markup.ids['password']
    assert 'persona' not in markup.ids and 'demo-form' not in markup.ids
    assert '/api/auth/demo-persona' not in javascript
    assert "request('/api/auth/login'" in javascript
    assert "username: $('username').value.trim(), password: $('password').value" in javascript
    assert 'provisioned demo account' in response.text


def test_navigation_is_role_separated(markup, javascript):
    for name in ['overview', 'users', 'departments', 'sql', 'provider', 'audit', 'compare', 'benchmark']:
        tab = markup.ids[f'tab-{name}']
        assert 'data-admin' in tab and 'hidden' in tab
        assert markup.ids[tab['aria-controls']]['role'] == 'tabpanel'
    for name in ['assistant', 'documents', 'profile']:
        assert 'data-admin' not in markup.ids[f'tab-{name}']
    assert "selectTab(data.user.role === 'admin' ? 'overview' : 'assistant')" in javascript
    assert "item.hidden = user?.role !== 'admin'" in javascript


def test_markup_ids_labels_and_script_boundaries(markup, javascript):
    ids = [attrs['id'] for _, attrs in markup.elements if 'id' in attrs]
    assert len(ids) == len(set(ids))
    for tag, attrs in markup.elements:
        assert not any(key.lower().startswith('on') for key in attrs)
        if tag == 'script':
            assert attrs.get('src', '').startswith('/static/')
        if 'for' in attrs:
            assert attrs['for'] in markup.ids
        for key in ['aria-controls', 'aria-labelledby']:
            if key in attrs:
                assert all(value in markup.ids for value in attrs[key].split())
    assert set(re.findall(r"\$\('([^']+)'\)", javascript)) <= markup.ids.keys()
    assert markup.ids['global-status']['role'] == 'status'
    assert markup.ids['chat-messages']['role'] == 'log'


def test_self_hosted_assets_csp_and_memory_only_session(client, javascript):
    response = client.get('/')
    csp = response.headers['content-security-policy']
    for directive in ["script-src 'self'", "style-src 'self'", "connect-src 'self'", "object-src 'none'"]:
        assert directive in csp
    assert response.headers['x-frame-options'] == 'DENY'
    for asset in ['app.js', 'styles.css']:
        assert client.get(f'/static/{asset}').status_code == 200
    assert not re.search(r'\b(?:localStorage|sessionStorage|indexedDB)\b', javascript)
    assert 'document.cookie' not in javascript
    assert not re.search(r'\b(?:innerHTML|outerHTML|insertAdjacentHTML)\b', javascript)
    assert not re.search(r'\beval\s*\(', javascript)
    assert "credentials: 'omit'" in javascript and "redirect: 'error'" in javascript
    assert "window.addEventListener('pagehide'" in javascript
    assert "window.addEventListener('pageshow'" in javascript
    assert 'state.token = null' in javascript


def test_document_contract_upload_search_and_permissions(markup, javascript):
    assert '/api/workspace/documents?' in javascript
    assert "new URLSearchParams({ q:" in javascript
    assert "status: $('document-status').value" in javascript
    upload = javascript.split("$('upload-form').addEventListener('submit'", 1)[1].split('function loadOverview', 1)[0]
    assert "form.append('file', file)" in upload
    assert upload.count('form.append(') == 1
    assert "request('/api/workspace/documents/upload'" in upload
    assert "body: { visibility: visibility.value, allowed_scopes }" in javascript
    for field in ['can_delete', 'can_reindex', 'has_original', 'allowed_scopes', 'owner_id', 'department', 'uploaded_at', 'filename', 'pages', 'chunks']:
        assert f'doc.{field}' in javascript
    for status in ['uploaded', 'processing', 'indexed', 'failed']:
        assert f'value="{status}"' in (STATIC / 'index.html').read_text()
    assert 'DOC_09' not in javascript


def test_authorized_blob_preview_and_cleanup(javascript):
    assert '/preview?page=${page}' in javascript
    assert '/download`' in javascript
    assert '{ blob: true }' in javascript
    assert 'await response.blob()' in javascript
    assert 'URL.createObjectURL(blob)' in javascript
    assert 'URL.revokeObjectURL(url)' in javascript
    close = javascript.split('function closeEvidence()', 1)[1].split('function showSource', 1)[0]
    assert "abortTask('preview')" in close and "abortTask('download')" in close
    assert 'revokeUrls()' in close
    assert 'image.alt =' in javascript
    assert '/api/documents/${encodeURIComponent(citation.document_id)}/chunks/${encodeURIComponent(citation.chunk_id)}' in javascript


def test_chat_history_selection_and_safe_rendering(javascript, markup):
    assert "request('/api/workspace/chat'" in javascript
    assert 'document_ids: state.selected.size ? [...state.selected] : null' in javascript
    assert 'conversation_id: state.conversation' in javascript
    assert "request('/api/workspace/conversations')" in javascript
    assert '/api/workspace/conversations/${encodeURIComponent(id)}' in javascript
    assert "state.conversation = null" in javascript
    assert 'chat.messages.map((item) => message(item.role, item.content, item))' in javascript
    assert 'navigator.clipboard.writeText' in javascript
    assert 'safeMarkdown(result.answer)' in javascript
    assert "node('li', item[1])" in javascript
    assert 'result.textContent = String(text)' in javascript
    assert 'new-chat' in markup.ids and 'chat-selected' in markup.ids


def test_request_cancellation_and_provider_truthfulness(javascript):
    assert 'new AbortController()' in javascript
    assert 'generation === state.generation' in javascript
    assert 'state.tasks.get(key) === task' in javascript
    assert "throw new DOMException('Superseded request', 'AbortError')" in javascript
    assert 'signal: task.controller.signal' in javascript
    assert 'diagnostics?.succeeded === true' in javascript
    assert 'diagnostics?.fallback_used === true' in javascript
    assert 'Local fallback' in javascript
    assert 'no confirmed model success' in javascript
    assert 'providerLabel(result.provider_status)' in javascript


def test_admin_api_shapes_and_blank_password_omission(javascript):
    for endpoint in ['/api/admin/overview', '/api/admin/users', '/api/admin/roles', '/api/admin/departments', '/api/admin/provider-status', '/api/admin/audit-logs', '/api/sql/query', '/api/eval/benchmark']:
        assert endpoint in javascript
    for field in ['active_users', 'inactive_users', 'active_admins', 'audit_logs', 'acl_epoch']:
        assert f"'{field}'" in javascript
    assert 'if (password.value) body.password = password.value' in javascript
    assert 'department_id: department.value || null' in javascript
    assert 'active: active.checked' in javascript
    assert "method: user ? 'PATCH' : 'POST'" in javascript
    assert "const body = {name:name.value.trim(),description:description.value.trim()}" in javascript
    assert 'data.successful_generations' in javascript and 'data.fallback_generations' in javascript
    assert 'data.last_generation' in javascript
    assert 'No generation has been recorded' in javascript


def test_responsive_and_keyboard_accessible(markup, javascript):
    css = (STATIC / 'styles.css').read_text()
    assert '@media (max-width: 700px)' in css
    assert '@media (prefers-reduced-motion: reduce)' in css
    assert ':focus-visible' in css
    assert '[hidden]' in css
    assert "'ArrowRight','ArrowDown'" in javascript
    assert "'ArrowLeft','ArrowUp'" in javascript
    assert "addEventListener('cancel'" in javascript
    assert markup.ids['evidence-dialog']['aria-labelledby'] == 'evidence-title'


@pytest.mark.parametrize('identity', ['general_user', 'admin_user'])
def test_product_seed_documents_are_indexed_and_selectable(client, javascript, identity):
    auth = headers(client, identity)
    legacy_before = client.get('/api/documents', headers=auth).json()['documents']
    with client.app.state.db.connect() as conn:
        legacy_ready = {row['id'] for row in conn.execute("SELECT id FROM documents WHERE status='ready'")}
    assert legacy_ready
    response = client.get('/api/workspace/documents', headers=auth)
    assert response.status_code == 200
    documents = response.json()['documents']
    visible_ready = {doc['id'] for doc in documents} & legacy_ready
    assert visible_ready and 'DOC_09' not in visible_ready
    assert all(doc['status'] == 'indexed' and doc['chunks'] > 0
               for doc in documents if doc['id'] in visible_ready)
    assert "check.disabled = doc.status !== 'indexed'" in javascript
    filtered = client.get('/api/workspace/documents?status=indexed', headers=auth)
    assert filtered.status_code == 200
    assert visible_ready <= {doc['id'] for doc in filtered.json()['documents']}
    with client.app.state.db.connect() as conn:
        assert legacy_ready == {row['id'] for row in conn.execute("SELECT id FROM documents WHERE status='ready'")}
    assert client.get('/api/documents', headers=auth).json()['documents'] == legacy_before


def test_product_indexed_filter_combines_legacy_and_native_statuses(isolated):
    auth = headers(isolated)
    states = {'DOC_01': 'ready', 'DOC_02': 'indexed', 'DOC_03': 'processing',
              'DOC_04': 'failed', 'DOC_05': 'uploaded'}
    with isolated.app.state.db.connect() as conn:
        conn.executemany('UPDATE documents SET status=? WHERE id=?',
                         [(value, key) for key, value in states.items()])
    for status in ['indexed', 'ready', 'processing', 'failed', 'uploaded']:
        response = isolated.get('/api/workspace/documents', params={'status': status}, headers=auth)
        assert response.status_code == 200
        documents = response.json()['documents']
        expected = {'DOC_01', 'DOC_02'} if status in {'indexed', 'ready'} else {
            key for key, value in states.items() if value == status}
        assert {doc['id'] for doc in documents} & states.keys() == expected
        assert all(doc['status'] == ('indexed' if status == 'ready' else status) for doc in documents)
        assert 'DOC_09' not in {doc['id'] for doc in documents}
    with isolated.app.state.db.connect() as conn:
        filename = conn.execute("SELECT filename FROM documents WHERE id='DOC_01'").fetchone()[0]
    searched = isolated.get('/api/workspace/documents', params={'status': 'indexed', 'q': filename}, headers=auth)
    assert {doc['id'] for doc in searched.json()['documents']} == {'DOC_01'}
    assert isolated.get('/api/workspace/documents?status=unknown', headers=auth).status_code == 422
