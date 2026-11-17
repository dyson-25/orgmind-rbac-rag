import re
from html.parser import HTMLParser

from app.rbac_guard import PERSONAS, SCOPES
from tests.test_orgmind_security import client, headers


class ScriptChecker(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inline_handlers = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.inline_handlers.extend(k for k in attributes if k.startswith('on'))
        if tag == 'script':
            self.scripts.append(attributes)


def test_fastapi_serves_dashboard_and_assets(client):
    response = client.get('/')
    assert response.status_code == 200
    assert 'text/html' in response.headers['content-type']
    assert 'OrgMind' in response.text
    assert 'synthetic' in response.text.lower()
    assert '/static/app.js' in response.text and '/static/styles.css' in response.text
    for asset in ['app.js','styles.css']:
        result = client.get('/static/' + asset)
        assert result.status_code == 200 and len(result.content) > 100


def test_csp_and_script_boundaries(client):
    response = client.get('/')
    assert "script-src 'self'" in response.headers['content-security-policy']
    assert response.headers['x-frame-options'] == 'DENY'
    parser = ScriptChecker(); parser.feed(response.text)
    assert not parser.inline_handlers
    assert parser.scripts and all(s.get('src','').startswith('/static/') for s in parser.scripts)
    js = client.get('/static/app.js').text
    assert not re.search(r'localStorage\s*\.', js)
    assert all(key not in js for key in ['AZURE_OPENAI_API_KEY','JWT_SECRET','GROQ_API_KEY'])


def test_personas_and_scope_ui_configuration_from_backend(client):
    response = client.get('/api/config')
    assert response.json()['personas'] == PERSONAS
    assert response.json()['scopes'] == list(SCOPES)
    assert response.headers['cache-control'] == 'no-store'
    me = client.get('/api/auth/me',headers=headers(client,'eng_user')).json()
    assert me['id'] == 'eng_user' and me['scopes'] == ['general','engineering']


def test_live_benchmark_api_contract(client):
    response = client.get('/api/eval/benchmark',headers=headers(client))
    assert response.status_code == 200
    result = response.json()
    assert result['rss_mb'] > 0
    assert result['case_count'] >= 12
    assert set(result['methods']) == {'bm25','lsa','hybrid'}
    for metrics in result['methods'].values():
        assert metrics['unauthorized_retrieval_rate'] == 0.0
        assert 0 <= metrics['recall_at_5'] <= 1
        assert 0 <= metrics['mrr'] <= 1
