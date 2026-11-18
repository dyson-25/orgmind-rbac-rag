"""Gate B regressions: every provider request is mocked, existing tests untouched."""

import json

import httpx
import pytest

from app import llm_service
from app.llm_service import HTTPGenerationAdapter, LLMService, SpyLLMRecorder
from app.rbac_guard import DENIAL


MODEL = 'gemini-3.8-flash'
BASE = 'https://generativelanguage.googleapis.com/v1beta/openai/'
CANARY = 'private-response-and-credential-canary'
ROW = {'id': 'travel-1', 'document_id': 'DOC_01', 'title': 'Travel policy',
       'page': 1, 'text': 'Travel reimbursement is available.', 'score': 0.9}
FIELDS = {'provider', 'model', 'http_status', 'error_category', 'succeeded', 'fallback_used'}


def reply(status=200, body=None):
    return httpx.Response(status, json=body if body is not None else {
        'choices': [{'message': {'content': 'Travel answer.'}, 'finish_reason': 'stop'}],
    }, request=httpx.Request('POST', BASE + 'chat/completions'))


@pytest.fixture(autouse=True)
def offline_gemini(monkeypatch):
    for name in ('AZURE_OPENAI_ENDPOINT', 'AZURE_OPENAI_API_KEY', 'AZURE_OPENAI_DEPLOYMENT',
                 'GROQ_API_KEY', 'GROQ_MODEL', 'GROQ_BASE_URL'):
        monkeypatch.delenv(name, raising=False)
    for name, value in {'GENERATION_PROVIDER': 'openai', 'GENERATION_TIMEOUT_SECONDS': '2.5',
                        'OPENAI_BASE_URL': BASE, 'OPENAI_MODEL': MODEL,
                        'OPENAI_API_KEY': CANARY}.items():
        monkeypatch.setenv(name, value)

    def unexpected(*args, **kwargs):
        pytest.fail('Unmocked provider network call')

    monkeypatch.setattr(httpx, 'post', unexpected)
    monkeypatch.setattr(httpx, 'get', unexpected)
    waits = []
    monkeypatch.setattr(llm_service.time, 'sleep', waits.append)
    return waits


def test_gemini_success_keeps_legacy_dict_and_spy_boundary(monkeypatch):
    requests = []

    def post(url, **kwargs):
        requests.append((url, kwargs))
        return reply()

    monkeypatch.setattr(httpx, 'post', post)
    adapter = HTTPGenerationAdapter('openai')
    spy = SpyLLMRecorder(adapter)
    service = LLMService(spy)
    result = service.generate('travel', [ROW])
    assert result == {
        'answer': 'Travel answer.', 'citations': [{'chunk_id': 'travel-1', 'document_id': 'DOC_01',
                                                  'title': 'Travel policy', 'page': 1}],
        'generation_mode': 'openai', 'injection_warning': False, 'provider_failed': False,
    }
    assert requests[0][0] == BASE + 'chat/completions'
    assert requests[0][1]['json'] == {'messages': spy.messages, 'model': MODEL}
    assert requests[0][1]['timeout'] == 2.5
    assert requests[0][1]['follow_redirects'] is False
    assert service.last_diagnostics == {
        'provider': 'gemini', 'model': MODEL, 'http_status': 200, 'error_category': None,
        'succeeded': True, 'fallback_used': False,
    }
    assert spy.last_diagnostics == adapter.last_diagnostics == service.last_diagnostics
    spy.last_diagnostics['model'] = 'changed'
    assert adapter.last_diagnostics['model'] == MODEL
    service.last_diagnostics['model'] = 'changed'
    assert adapter.last_diagnostics['model'] == MODEL
    assert CANARY not in json.dumps(spy.calls)


@pytest.mark.parametrize('status,category,attempts', [
    (400, 'config', 1), (401, 'auth', 1), (403, 'permission', 1),
    (404, 'model', 1), (408, 'timeout', 1), (422, 'config', 1),
    (429, 'quota', 3), (500, 'service', 1), (502, 'service', 1),
    (503, 'service', 3), (504, 'timeout', 1), (307, 'config', 1),
])
def test_exact_http_status_safe_category_and_retry_bound(monkeypatch, offline_gemini, status, category, attempts):
    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs)
        response = reply(status, {'error': {'message': CANARY}})
        response.headers['Retry-After'] = '999999999'
        return response

    monkeypatch.setattr(httpx, 'post', post)
    service = LLMService(SpyLLMRecorder(HTTPGenerationAdapter('openai')))
    result = service.generate('travel', [ROW])
    assert result['answer'] == ROW['text']
    assert result['provider_failed'] is True
    assert service.last_diagnostics == {
        'provider': 'gemini', 'model': MODEL, 'http_status': status, 'error_category': category,
        'succeeded': False, 'fallback_used': True,
    }
    assert len(calls) == attempts
    assert offline_gemini == ([0.25, 0.5] if attempts == 3 else [])
    assert CANARY not in json.dumps([result, service.last_diagnostics])


@pytest.mark.parametrize('status', [429, 503])
def test_retry_recovery_and_last_call_reset(monkeypatch, offline_gemini, status):
    responses = iter([reply(status), reply(), reply(403), reply()])
    monkeypatch.setattr(httpx, 'post', lambda *a, **k: next(responses))
    service = LLMService()
    service.generate('travel', [ROW])
    first = service.last_diagnostics
    assert first['succeeded'] is True
    assert first['http_status'] == 200
    assert offline_gemini == [0.25]
    service.generate('travel', [ROW])
    failed = service.last_diagnostics
    assert failed['error_category'] == 'permission'
    assert failed['succeeded'] is False
    assert failed['fallback_used'] is True
    service.generate('travel', [ROW])
    assert service.last_diagnostics == first
    assert service.last_diagnostics is not first
    assert failed['http_status'] == 403


@pytest.mark.parametrize('error,category', [
    (httpx.ReadTimeout(CANARY), 'timeout'),
    (httpx.ConnectError(CANARY), 'service'),
    (httpx.UnsupportedProtocol(CANARY), 'config'),
    (httpx.InvalidURL(CANARY), 'config'),
    (RuntimeError(CANARY), 'service'),
])
def test_transport_errors_never_escape_or_leak(monkeypatch, error, category):
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        raise error

    monkeypatch.setattr(httpx, 'post', post)
    adapter = HTTPGenerationAdapter('openai')
    with pytest.raises(RuntimeError, match='^Generation provider unavailable$') as caught:
        adapter.generate([])
    assert caught.value.__suppress_context__
    assert CANARY not in str(caught.value)
    assert adapter.last_diagnostics['error_category'] == category
    assert adapter.last_diagnostics['http_status'] is None
    assert len(calls) == 1


def test_timeout_after_retry_does_not_retain_previous_http_status(monkeypatch):
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return reply(503)
        raise httpx.ReadTimeout(CANARY)

    monkeypatch.setattr(httpx, 'post', post)
    service = LLMService()
    service.generate('travel', [ROW])
    assert service.last_diagnostics['http_status'] is None
    assert service.last_diagnostics['error_category'] == 'timeout'
    assert len(calls) == 2


@pytest.mark.parametrize('body', [
    {}, {'choices': []}, {'choices': [None]}, {'choices': [{'message': {'content': ''}}]},
    {'choices': [{'message': {'content': 12}}]},
    {'choices': [{'message': {'content': CANARY}, 'finish_reason': 'length'}]},
    {'choices': [{'message': {'content': CANARY, 'tool_calls': [{'id': CANARY}]}}]},
    {'choices': [{'message': {'content': CANARY, 'function_call': {'name': CANARY}}}]},
])
def test_malformed_completion_retains_200_without_raw_content(monkeypatch, body):
    monkeypatch.setattr(httpx, 'post', lambda *a, **k: reply(body=body))
    service = LLMService()
    result = service.generate('travel', [ROW])
    assert service.last_diagnostics['http_status'] == 200
    assert service.last_diagnostics['error_category'] == 'malformed'
    assert service.last_diagnostics['succeeded'] is False
    assert service.last_diagnostics['fallback_used'] is True
    assert CANARY not in json.dumps([result, service.last_diagnostics])


def test_non_json_success_is_malformed(monkeypatch):
    monkeypatch.setattr(httpx, 'post', lambda *a, **k: httpx.Response(200, text=CANARY))
    service = LLMService()
    service.generate('travel', [ROW])
    assert service.last_diagnostics['http_status'] == 200
    assert service.last_diagnostics['error_category'] == 'malformed'


@pytest.mark.parametrize('name,value', [
    ('OPENAI_API_KEY', ''), ('OPENAI_MODEL', ''), ('GENERATION_TIMEOUT_SECONDS', 'nan'),
    ('OPENAI_BASE_URL', 'https://bad.invalid/?secret=private'),
    ('OPENAI_BASE_URL', 'https://user:password@bad.invalid'),
    ('OPENAI_BASE_URL', 'http://generativelanguage.googleapis.com/v1beta/openai/'),
    ('OPENAI_BASE_URL', 'invalid'),
])
def test_configuration_fails_without_network(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    service = LLMService()
    service.generate('travel', [ROW])
    assert service.last_diagnostics['error_category'] == 'config'
    assert service.last_diagnostics['http_status'] is None
    assert service.last_diagnostics['succeeded'] is False
    if name == 'OPENAI_API_KEY':
        assert service.last_diagnostics['provider'] == 'gemini'
        assert service.last_diagnostics['model'] == MODEL


@pytest.mark.parametrize('base', ['https://compatible.invalid/v1/',
                                  'https://generativelanguage.googleapis.com.evil.invalid/v1/'])
def test_generic_payload_is_unchanged_and_not_retried(monkeypatch, base):
    monkeypatch.setenv('OPENAI_BASE_URL', base)
    requests = []

    def post(url, **kwargs):
        requests.append(kwargs)
        return reply(429)

    monkeypatch.setattr(httpx, 'post', post)
    service = LLMService()
    service.generate('travel', [ROW])
    assert len(requests) == 1
    assert requests[0]['json']['temperature'] == 0
    assert service.last_diagnostics['provider'] == 'openai'


def test_local_spy_string_adapter_and_empty_context_semantics(monkeypatch):
    monkeypatch.setenv('GENERATION_PROVIDER', 'local')
    for service in (LLMService(), LLMService(SpyLLMRecorder())):
        assert set(service.last_diagnostics) == FIELDS
        service.generate('travel', [ROW])
        assert service.last_diagnostics['error_category'] is None
        assert service.last_diagnostics['succeeded'] is False
        assert service.last_diagnostics['fallback_used'] is True

    class StringAdapter:
        provider = 'mock-model'

        def generate(self, messages):
            return 'Travel answer.'

    service = LLMService(SpyLLMRecorder(StringAdapter()))
    assert service.generate('travel', [ROW])['generation_mode'] == 'mock-model'
    assert service.last_diagnostics['succeeded'] is True
    assert service.last_diagnostics['fallback_used'] is False
    assert service.last_diagnostics['provider'] == 'custom'
    assert service.last_diagnostics['http_status'] is None

    monkeypatch.setattr(httpx, 'post', lambda *a, **k: reply())
    adapter = HTTPGenerationAdapter('openai')
    service = LLMService(adapter)
    assert service.generate('unrelated', [])['answer'] == DENIAL
    assert service.last_diagnostics['succeeded'] is True
    assert service.last_diagnostics['fallback_used'] is True
    assert adapter.last_diagnostics['fallback_used'] is False


def test_unsafe_model_metadata_is_not_exposed(monkeypatch):
    monkeypatch.setenv('OPENAI_MODEL', 'https://user:password@host/?key=private')
    monkeypatch.setattr(httpx, 'post', lambda *a, **k: reply(400, {'error': CANARY}))
    service = LLMService()
    service.generate('travel', [ROW])
    assert service.last_diagnostics['model'] is None
    assert 'password' not in json.dumps(service.last_diagnostics)
