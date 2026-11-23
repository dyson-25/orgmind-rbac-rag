import json

import httpx
import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.llm_service import GenerationError, GenerationService
from app.main import SAMPLE, create_app

HEADERS = {'X-OrgMind-Request': '1'}


def make_pdf(text):
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_textbox(pymupdf.Rect(40, 40, 550, 800), text)
        return pdf.tobytes()


class EvidenceCapture:
    def __init__(self):
        self.documents = set()

    def answer(self, question, chunks, history=None):
        self.documents = {chunk['filename'] for chunk in chunks}
        citations = [{'document_id': chunk['document_id'], 'chunk_id': chunk['id'],
                      'filename': chunk['filename'], 'page': chunk['page'], 'excerpt': chunk['text']}
                     for chunk in chunks]
        answer = '\n'.join(f"{chunk['filename']}: {chunk['text']}" for chunk in chunks)
        return {'status': 'generated', 'answer': answer, 'citations': citations}


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, requests_per_minute=120)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), headers=HEADERS) as client:
        yield client


def test_startup_is_empty_and_public_config_is_safe(client):
    assert client.get('/api/health').json() == {'status': 'ok'}
    response = client.get('/api/config')
    assert response.json()['session_ttl_days'] == 30
    assert 'authenticated' not in response.json()
    assert 'password_required' not in response.json()
    assert response.json()['generation_available'] is False
    assert 'orgmind_session' in client.cookies
    assert client.get('/api/documents').json() == {'documents': []}
    assert 'HttpOnly' in response.headers['set-cookie']
    assert response.headers['cache-control'] == 'no-store'
    assert 'api_key' not in response.text
    assert 'app_password' not in response.text
    assert client.get('/').status_code == 200
    assert client.get('/api/health').headers['x-content-type-options'] == 'nosniff'


def test_sample_is_optional_deduplicated_inspectable_and_deletable(client):
    assert list(SAMPLE.parent.glob('*.pdf')) == [SAMPLE]
    doc = client.post('/api/documents/sample').json()
    assert doc['is_sample'] is True
    assert doc['pages'] == 4 and doc['status'] == 'indexed'
    assert client.post('/api/documents/sample').json()['id'] == doc['id']
    assert len(client.get('/api/documents').json()['documents']) == 1
    with client.app.state.db.connect() as conn:
        chunks = [dict(row) for row in conn.execute('SELECT * FROM chunks WHERE document_id=?', (doc['id'],))]
    assert chunks and {chunk['page'] for chunk in chunks} == {1, 2, 3, 4}
    for chunk in chunks:
        result = client.get(f"/api/documents/{doc['id']}/sources/{chunk['id']}")
        assert result.status_code == 200
        assert result.json()['text'] == chunk['text']
    preview = client.get(f"/api/documents/{doc['id']}/preview?page=2")
    assert preview.headers['content-type'] == 'image/png'
    assert preview.content.startswith(b'\x89PNG')
    assert client.get(f"/api/documents/{doc['id']}/preview?page=99").status_code == 404
    original = client.app.state.db.original_path(doc['id'])
    assert client.delete(f"/api/documents/{doc['id']}").status_code == 200
    assert not original.exists()
    assert client.app.state.rag.retrieve('emergency reserve') == []
    assert client.get(f"/api/documents/{doc['id']}/sources/{chunks[0]['id']}").status_code == 404
    assert client.get(f"/api/documents/{doc['id']}/preview").status_code == 404
    assert client.delete(f"/api/documents/{doc['id']}").status_code == 404


def test_suggestions_are_document_scoped_and_workspace_private(client, settings):
    raw = make_pdf('Solar panels have a measured output of 3 kW. The inverter converts electricity. '
                   'Monthly generation is 120 kWh and maintenance is listed in a table.')
    doc = client.post('/api/documents/upload', files={'file': ('solar.pdf', raw)}).json()
    response = client.get('/api/suggestions', params=[('document_ids', doc['id'])])
    assert response.status_code == 200
    questions = [item['question'] for item in response.json()['suggestions']]
    assert questions[0] == 'What is this document about?'
    assert any('numbers' in question for question in questions)
    assert any(any(term in question.casefold() for term in ('solar', 'inverter', 'electricity')) for question in questions)
    assert client.get('/api/suggestions', params=[('document_ids', 'missing')]).status_code == 404
    assert client.get('/api/suggestions').status_code == 400
    with TestClient(create_app(settings), headers=HEADERS) as other:
        assert other.get('/api/suggestions', params=[('document_ids', doc['id'])]).status_code == 404


@pytest.fixture
def selected_pdf_pair(client):
    assignment = client.post('/api/documents/upload', files={'file': ('Assignment DM.pdf', make_pdf(
        'Assignment DM explains five coursework topics and their announcement and submission dates.'))}).json()
    solar = client.post('/api/documents/upload', files={'file': ('Solar Energy.pdf', make_pdf(
        'Solar Energy explains photovoltaic panels, inverters, and household electricity generation.'))}).json()
    capture = EvidenceCapture()
    client.app.state.generator = capture
    return assignment, solar, capture


def _ask_selected(client, docs):
    return client.post('/api/chat', json={
        'query': 'Explain the selected documents separately.',
        'document_ids': [doc['id'] for doc in docs],
    })


def test_chat_uses_both_selected_documents(selected_pdf_pair, client):
    assignment, solar, capture = selected_pdf_pair
    result = _ask_selected(client, [assignment, solar])
    assert result.status_code == 200
    assert capture.documents == {'Assignment DM.pdf', 'Solar Energy.pdf'}
    assert 'Assignment DM.pdf' in result.json()['answer']
    assert 'Solar Energy.pdf' in result.json()['answer']
    assert {citation['filename'] for citation in result.json()['citations']} == capture.documents


def test_chat_with_only_assignment_excludes_solar(selected_pdf_pair, client):
    assignment, solar, capture = selected_pdf_pair
    result = _ask_selected(client, [assignment])
    assert result.status_code == 200
    assert capture.documents == {'Assignment DM.pdf'}
    assert 'Solar Energy.pdf' not in result.json()['answer']


def test_chat_with_only_solar_excludes_assignment(selected_pdf_pair, client):
    assignment, solar, capture = selected_pdf_pair
    result = _ask_selected(client, [solar])
    assert result.status_code == 200
    assert capture.documents == {'Solar Energy.pdf'}
    assert 'Assignment DM.pdf' not in result.json()['answer']


def test_unchecked_pdf_is_excluded_from_chat_sources(selected_pdf_pair, client):
    assignment, solar, capture = selected_pdf_pair
    checked_ids = {assignment['id'], solar['id']}
    unchecked_id = solar['id']
    selected_ids = checked_ids - {unchecked_id}
    result = _ask_selected(client, [doc for doc in (assignment, solar) if doc['id'] in selected_ids])
    assert result.status_code == 200
    assert capture.documents == {'Assignment DM.pdf'}
    assert 'Solar Energy.pdf' not in result.json()['answer']


def test_multi_document_overview_reaches_every_selected_document(client):
    client.app.state.settings.upload_requests_per_minute = 10
    client.app.state.settings.ip_upload_requests_per_minute = 10
    documents = []
    for index in range(6):
        filename = f'Document {index + 1}.pdf'
        raw = make_pdf(f'Distinctive evidence for document {index + 1}: topic{index + 1}.')
        documents.append(client.post('/api/documents/upload', files={'file': (filename, raw)}).json())

    capture = EvidenceCapture()
    client.app.state.generator = capture
    result = _ask_selected(client, documents)

    assert result.status_code == 200
    expected = {doc['filename'] for doc in documents}
    assert capture.documents == expected
    assert {citation['filename'] for citation in result.json()['citations']} == expected


def test_upload_and_data_survive_restart(settings):
    raw = make_pdf('A botanical garden waters its orchid collection every Tuesday morning.')
    with TestClient(create_app(settings), headers=HEADERS) as client:
        result = client.post('/api/documents/upload', files={'file': ('garden.pdf', raw, 'application/pdf')})
        assert result.status_code == 200, result.text
        doc = result.json()
        assert doc['status'] == 'indexed' and doc['is_sample'] is False
        cookie = client.cookies.get('orgmind_session')
    with TestClient(create_app(settings), headers=HEADERS) as client:
        assert client.get('/api/documents').json()['documents'] == []
        client.cookies.set('orgmind_session', cookie, domain='testserver.local', path='/')
        assert client.get('/api/documents').json()['documents'] == [doc]
        chunks = client.app.state.rag.retrieve('When is the orchid collection watered?')
        assert chunks and chunks[0]['document_id'] == doc['id']
        assert 'Tuesday' in chunks[0]['text']
        assert client.get(f"/api/documents/{doc['id']}/preview").status_code == 200


@pytest.mark.parametrize('filename,raw', [('bad.txt', b'hello'), ('bad.pdf', b'not a pdf')])
def test_invalid_upload(client, filename, raw):
    assert client.post('/api/documents/upload', files={'file': (filename, raw)}).status_code == 400
    assert client.get('/api/documents').json()['documents'] == []


def test_upload_file_limit_is_enforced(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, max_upload_bytes=100)), headers=HEADERS) as client:
        assert client.post('/api/documents/upload', files={'file': ('large.pdf', b'x' * 101)}).status_code == 413
        assert client.post('/api/documents/upload', content=b'x' * 66000).status_code == 413


def test_json_body_limit_and_validation(client):
    assert client.post('/api/chat', content=b'x' * 17000).status_code == 413
    for payload in [{'query': ''}, {'query': ' '}, {'query': 'x' * 2001}, {'query': 'budget', 'unexpected': 'sensitive'}]:
        result = client.post('/api/chat', json=payload)
        assert result.status_code == 422
        assert 'sensitive' not in result.text
    assert client.post('/api/chat', json={'query': 'budget', 'document_ids': []}).status_code == 400
    assert client.post('/api/chat', json={'query': 'budget', 'document_ids': ['missing']}).status_code == 404


def test_empty_library_returns_insufficient_evidence(client):
    result = client.post('/api/chat', json={'query': 'What are the savings targets?'}).json()
    assert result['status'] == 'insufficient_evidence'
    assert result['citations'] == []
    assert result['conversation_id']


def test_unconfigured_generation_is_honest(client):
    client.post('/api/documents/sample')
    result = client.post('/api/chat', json={'query': 'What is the monthly take-home income?'})
    assert result.status_code == 503
    assert 'answer' not in result.json()


def test_real_generation_layer_integrates_with_retrieved_citations_and_followup(settings):
    requests = []

    def provider(request):
        data = json.loads(json.loads(request.content)['messages'][1]['content'])
        requests.append(data)
        chunks = data['retrieved_sources']
        evidence = next(chunk for chunk in chunks if '$4,500' in chunk['text'])
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({
            'answer': 'The three-month emergency reserve target is $4,500.', 'citation_ids': [evidence['id']],
        })}}]})

    settings.generation_provider = 'openai'
    settings.openai_api_key = 'offline-test-key'
    with httpx.Client(transport=httpx.MockTransport(provider)) as transport:
        app = create_app(settings, GenerationService(settings, transport))
        with TestClient(app, headers=HEADERS) as client:
            doc = client.post('/api/documents/sample').json()
            answer = client.post('/api/chat', json={'query': 'What is the three-month emergency reserve target?', 'document_ids': [doc['id']]}).json()
            assert answer['status'] == 'generated'
            citation = answer['citations'][0]
            source = client.get(f"/api/documents/{doc['id']}/sources/{citation['chunk_id']}").json()
            assert citation['document_id'] == doc['id']
            assert citation['page'] == source['page']
            assert citation['excerpt'] == source['text']
            assert '$4,500' in source['text']
            with pymupdf.open(SAMPLE) as pdf:
                assert '$4,500' in pdf[citation['page'] - 1].get_text()
            followup = client.post('/api/chat', json={'query': 'And how much is that?', 'document_ids': [doc['id']], 'conversation_id': answer['conversation_id']})
            assert followup.status_code == 200, followup.text
            assert len(requests) == 2
            assert 'three-month emergency reserve' in json.dumps(requests[1])
            assert client.post('/api/conversations/clear', json={'conversation_id': answer['conversation_id']}).status_code == 200
            assert client.post('/api/chat', json={'query': 'More?', 'conversation_id': answer['conversation_id']}).status_code == 404


def test_provider_rate_limit_message_and_retry_after_are_sanitized(settings):
    def provider(_request):
        return httpx.Response(429, text='private provider detail', headers={'Retry-After': '45'})

    settings.generation_provider = 'openai'
    settings.openai_api_key = 'offline-test-key'
    with httpx.Client(transport=httpx.MockTransport(provider)) as transport:
        app = create_app(settings, GenerationService(settings, transport))
        with TestClient(app, headers=HEADERS) as client:
            doc = client.post('/api/documents/upload', files={'file': ('notes.pdf', make_pdf('Measured output is 3 kW.'))}).json()
            response = client.post('/api/chat', json={'query': 'What is the output?', 'document_ids': [doc['id']]})
            assert response.status_code == 429
            assert response.headers['retry-after'] == '45'
            assert response.json() == {'detail': 'Answer generation is temporarily busy. Your documents are safe. Please try again shortly.'}
            assert 'private provider detail' not in response.text
            assert client.get('/api/documents').json()['documents'][0]['id'] == doc['id']


def test_provider_error_does_not_create_conversation(client):
    class Failing:
        def answer(self, *args, **kwargs):
            raise GenerationError('Provider temporarily unavailable.', 503, 'unavailable')
    client.app.state.generator = Failing()
    result = client.post('/api/chat', json={'query': 'budget'})
    assert result.status_code == 503
    assert not any(client.app.state.sessions.chats.values())


def test_hosted_public_access_issues_secure_persistent_cookie(tmp_path):
    settings = Settings(data_dir=tmp_path, environment='production')
    with TestClient(create_app(settings), base_url='https://testserver', headers=HEADERS) as client:
        assert client.get('/api/health').status_code == 200
        response = client.get('/')
        assert response.status_code == 200
        cookie = response.headers['set-cookie']
        assert 'Secure' in cookie and 'SameSite=strict' in cookie and 'HttpOnly' in cookie
        assert 'Max-Age=2592000' in cookie
        token = client.cookies.get('orgmind_session')
        assert token and len(token) >= 43
        assert client.get('/api/documents').json() == {'documents': []}
        assert client.get('/api/config').json()['session_ttl_days'] == 30
        assert client.cookies.get('orgmind_session') == token
        for path in ['/api/documents/no/preview', '/api/documents/no/sources/no']:
            assert client.get(path).status_code == 404
        assert client.post('/api/chat', json={'query': 'budget'}).status_code == 200
        assert client.delete('/api/documents/no').status_code == 404
        assert client.post('/api/documents/sample').status_code == 200
        for path in ['/api/unlock', '/api/lock', '/api/login']:
            assert client.post(path, json={}).status_code == 404


def test_mutations_require_application_header_and_same_origin(client):
    assert client.post('/api/documents/sample', headers={'X-OrgMind-Request': ''}).status_code == 403
    assert client.post('/api/documents/sample', headers={'Origin': 'https://other.invalid'}).status_code == 403
    assert client.post('/api/conversations/clear', json={}, headers={'Origin': 'http://testserver'}).status_code == 200


def test_new_workspace_rate_limit_cannot_be_bypassed_by_cookie_rotation(client):
    for _ in range(10):
        client.cookies.clear()
        assert client.get('/api/config').status_code == 200
    client.cookies.clear()
    result = client.get('/api/config')
    assert result.status_code == 429 and result.headers['retry-after'] == '60'


def test_generation_rate_limit(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, requests_per_minute=1)), headers=HEADERS) as client:
        assert client.post('/api/chat', json={'query': 'budget'}).status_code == 200
        assert client.post('/api/chat', json={'query': 'budget'}).status_code == 429


def test_deletion_invalidates_conversations(client):
    doc = client.post('/api/documents/sample').json()
    answer = client.post('/api/chat', json={'query': 'quantum chromodynamics'}).json()
    assert answer['status'] == 'insufficient_evidence'
    client.delete(f"/api/documents/{doc['id']}")
    assert client.post('/api/chat', json={'query': 'explain more', 'conversation_id': answer['conversation_id']}).status_code == 404


@pytest.mark.parametrize('path', ['/api/login', '/api/workspace/documents', '/api/accounts', '/api/evaluate', '/api/sql', '/docs', '/openapi.json'])
def test_obsolete_routes_are_absent(client, path):
    assert client.get(path).status_code == 404
