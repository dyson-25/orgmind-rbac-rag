"""The manual diagnostic utility is verified without keys or network access."""

import getpass
import warnings

import httpx
import pytest

from app import llm_service
from scripts import diagnose_gemini as cli


CANARY = 'synthetic-prompt-key-never-print'
BODY_CANARY = 'upstream-body-never-print'


def response(status=200, body=None):
    return httpx.Response(status, json=body if body is not None else {'error': BODY_CANARY})


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail('Unexpected network call')

    monkeypatch.setattr(httpx, 'get', unexpected)
    monkeypatch.setattr(httpx, 'post', unexpected)
    monkeypatch.setattr(getpass, 'getpass', lambda prompt: CANARY)
    monkeypatch.setenv('OPENAI_API_KEY', 'must-not-use-environment-key')
    monkeypatch.setenv('GEMINI_API_KEY', 'must-not-use-environment-key')
    monkeypatch.setenv('GOOGLE_API_KEY', 'must-not-use-environment-key')
    waits = []
    monkeypatch.setattr(llm_service.time, 'sleep', waits.append)
    return waits


def access_response():
    return response(body={'name': f'models/{cli.MODEL}'})


def completion_response():
    return response(body={'choices': [{'message': {'content': BODY_CANARY}, 'finish_reason': 'stop'}]})


def test_prompt_only_get_model_and_small_completion(monkeypatch, capsys):
    calls = []

    def get(url, **kwargs):
        calls.append(('get', url, kwargs))
        return access_response()

    def post(url, **kwargs):
        calls.append(('post', url, kwargs))
        return completion_response()

    monkeypatch.setattr(httpx, 'get', get)
    monkeypatch.setattr(httpx, 'post', post)
    assert cli.main([]) == 0
    assert [call[0] for call in calls] == ['get', 'post']
    assert calls[0][1] == 'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash'
    assert calls[0][2]['headers'] == {'x-goog-api-key': CANARY}
    assert calls[1][1] == cli.BASE_URL + 'chat/completions'
    assert calls[1][2]['headers'] == {'Authorization': f'Bearer {CANARY}'}
    assert calls[1][2]['json'] == {
        'model': 'gemini-3.8-flash', 'messages': [{'role': 'user', 'content': 'Reply with OK.'}],
        'max_tokens': 128,
    }
    assert all(call[2]['timeout'] == 15.0 and call[2]['follow_redirects'] is False for call in calls)
    assert all(CANARY not in call[1] for call in calls)
    output = capsys.readouterr()
    assert output.out.count('http_status=200') == 2
    assert output.out.count('succeeded=true fallback_used=false') == 2
    assert CANARY not in output.out + output.err
    assert BODY_CANARY not in output.out + output.err
    assert output.err == ''


@pytest.mark.parametrize('status,category,attempts', [
    (401, 'auth', 1), (403, 'permission', 1), (404, 'model', 1),
    (429, 'quota', 3), (503, 'service', 3), (400, 'config', 1),
])
def test_access_failure_has_exact_safe_status_and_stops(monkeypatch, capsys, offline, status, category, attempts):
    calls = []

    def get(*args, **kwargs):
        calls.append(1)
        result = response(status)
        result.headers['Retry-After'] = '100000'
        return result

    monkeypatch.setattr(httpx, 'get', get)
    assert cli.main([]) == 1
    assert len(calls) == attempts
    assert offline == ([0.25, 0.5] if attempts == 3 else [])
    output = capsys.readouterr()
    assert f'http_status={status} error_category={category} succeeded=false fallback_used=false' in output.out
    assert 'stage=completion' not in output.out
    assert BODY_CANARY not in output.out + output.err
    assert CANARY not in output.out + output.err


@pytest.mark.parametrize('status,category,attempts', [(403, 'permission', 1), (429, 'quota', 3), (503, 'service', 3)])
def test_completion_failure_is_not_reported_as_success(monkeypatch, capsys, status, category, attempts):
    monkeypatch.setattr(httpx, 'get', lambda *a, **k: access_response())
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        return response(status)

    monkeypatch.setattr(httpx, 'post', post)
    assert cli.main([]) == 1
    output = capsys.readouterr().out
    assert len(calls) == attempts
    assert f'http_status={status} error_category={category} succeeded=false' in output
    assert 'stage=access' in output and 'stage=completion' in output
    assert BODY_CANARY not in output


def test_both_stages_can_recover_with_bounded_retries(monkeypatch, capsys, offline):
    gets = iter([response(429), access_response()])
    posts = iter([response(503), completion_response()])
    monkeypatch.setattr(httpx, 'get', lambda *a, **k: next(gets))
    monkeypatch.setattr(httpx, 'post', lambda *a, **k: next(posts))
    assert cli.main([]) == 0
    assert offline == [0.25, 0.25]
    assert capsys.readouterr().out.count('http_status=200 error_category=none succeeded=true') == 2


@pytest.mark.parametrize('stage', ['access', 'completion'])
@pytest.mark.parametrize('failure,category', [('timeout', 'timeout'), ('network', 'service'),
                                             ('json', 'malformed'), ('shape', 'malformed')])
def test_safe_exceptions_and_invalid_responses(monkeypatch, capsys, stage, failure, category):
    def fail(*args, **kwargs):
        if failure == 'timeout':
            raise httpx.ReadTimeout(BODY_CANARY)
        if failure == 'network':
            raise httpx.ConnectError(BODY_CANARY)
        if failure == 'json':
            return httpx.Response(200, text=BODY_CANARY)
        return response(body={'name': 'models/wrong-model', 'choices': []})

    if stage == 'completion':
        monkeypatch.setattr(httpx, 'get', lambda *a, **k: access_response())
    monkeypatch.setattr(httpx, 'get' if stage == 'access' else 'post', fail)
    assert cli.main([]) == 1
    output = capsys.readouterr()
    assert f'error_category={category} succeeded=false' in output.out
    assert ('http_status=200' if category == 'malformed' else 'http_status=none') in output.out
    assert BODY_CANARY not in output.out + output.err
    assert CANARY not in output.out + output.err


@pytest.mark.parametrize('arguments', [['--key', CANARY], [CANARY], ['--help']])
def test_arguments_rejected_without_echo_or_prompt(monkeypatch, capsys, arguments):
    monkeypatch.setattr(getpass, 'getpass', lambda *a: pytest.fail('Must not prompt for invalid arguments'))
    assert cli.main(arguments) == 2
    output = capsys.readouterr()
    assert 'stage=input' in output.out
    assert 'error_category=config' in output.out
    assert CANARY not in output.out + output.err


@pytest.mark.parametrize('failure', ['empty', 'eof', 'interrupt', 'non_tty'])
def test_prompt_failure_never_uses_echoing_input(monkeypatch, capsys, failure):
    def prompt(*args, **kwargs):
        if failure == 'empty':
            return '  '
        if failure == 'eof':
            raise EOFError(BODY_CANARY)
        if failure == 'interrupt':
            raise KeyboardInterrupt()
        warnings.warn(BODY_CANARY, getpass.GetPassWarning)
        pytest.fail('Non-TTY fallback must not proceed')

    monkeypatch.setattr(getpass, 'getpass', prompt)
    monkeypatch.setattr('builtins.input', lambda *a: pytest.fail('Echoing input forbidden'))
    assert cli.main([]) == 2
    output = capsys.readouterr()
    assert 'error_category=config' in output.out
    assert BODY_CANARY not in output.out + output.err
    assert output.err == ''
