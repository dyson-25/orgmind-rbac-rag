import asyncio

import pytest

from app.config import Settings
from app.http_limits import RequestBodyLimit


@pytest.mark.parametrize('options', [
    {'session_ttl_days': 0}, {'session_ttl_days': 91},
    {'upload_requests_per_minute': 0}, {'upload_requests_per_minute': 121},
    {'ip_upload_requests_per_minute': 0}, {'ip_upload_requests_per_minute': 121},
    {'global_requests_per_minute': 0}, {'global_requests_per_minute': 121},
    {'max_total_documents': 0}, {'max_total_documents': 5001},
    {'generation_max_tokens': 127}, {'generation_max_tokens': 8193},
    {'environment': 'unknown'}, {'max_upload_bytes': 0},
    {'max_upload_bytes': 20 * 1024 * 1024 + 1},
    {'max_documents': 0}, {'max_documents': 101},
    {'max_query_chars': 0}, {'max_query_chars': 4001},
    {'requests_per_minute': 0}, {'requests_per_minute': 121},
    {'chunk_size': 99}, {'chunk_size': 4001}, {'chunk_overlap': 900},
    {'generation_provider': 'unsupported'},
    {'openai_base_url': 'https://untrusted.invalid/v1'},
    {'openai_model': ''}, {'generation_timeout_seconds': float('nan')},
    {'generation_timeout_seconds': float('inf')}, {'generation_timeout_seconds': 0},
])
def test_settings_reject_invalid_boundaries(options):
    with pytest.raises(ValueError):
        Settings(**options)


def test_production_settings_keep_secrets_out_of_repr(tmp_path):
    settings = Settings(data_dir=tmp_path, environment='production', openai_api_key='private-test-key')
    assert settings.db_path == tmp_path / 'runtime' / 'orgmind.sqlite3'
    assert not hasattr(settings, 'app_password')
    assert settings.openai_api_key not in repr(settings)


def run_request(chunks, headers=(), path='/api/chat', disconnect=False):
    seen, output = [], []
    messages = [{'type': 'http.request', 'body': chunk, 'more_body': index < len(chunks) - 1 or disconnect}
                for index, chunk in enumerate(chunks)]
    messages.append({'type': 'http.disconnect'})

    async def receive():
        return messages.pop(0)

    async def send(message):
        output.append(message)

    async def application(scope, receive, send):
        seen.append(await receive())
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok'})

    asyncio.run(RequestBodyLimit(application, max_bytes=100)(
        {'type': 'http', 'method': 'POST', 'path': path, 'headers': list(headers)}, receive, send,
    ))
    return seen, output


@pytest.mark.parametrize('headers', [(), ((b'content-length', b'1'),)])
def test_chunked_json_cannot_bypass_size_limit(headers):
    seen, output = run_request([b'x' * 10000, b'y' * 7000], headers)
    assert not seen
    assert output[0]['status'] == 413


@pytest.mark.parametrize('length', [b'invalid', b'-1', b'17000'])
def test_invalid_declared_length_rejected_before_parsing(length):
    seen, output = run_request([b'{}'], ((b'content-length', length),))
    assert not seen
    assert output[0]['status'] == 413


def test_allowed_chunked_json_delivered_once():
    seen, output = run_request([b'{', b'}'])
    assert seen == [{'type': 'http.request', 'body': b'{}', 'more_body': False}]
    assert output[0]['status'] == 200


def test_disconnected_upload_never_reaches_parser():
    seen, output = run_request([b'partial'], path='/api/documents/upload', disconnect=True)
    assert seen == output == []


def test_chunked_upload_limit_includes_bounded_multipart_overhead():
    seen, output = run_request([b'x' * 65000, b'y' * 1000], path='/api/documents/upload')
    assert not seen
    assert output[0]['status'] == 413
