from app.llm_service import LLMService
from tests.test_orgmind_security import client, headers


def test_provider_status_is_admin_only(client):
    assert client.get('/api/admin/provider-status').status_code == 401
    assert client.get('/api/admin/provider-status', headers=headers(client, 'general_user')).status_code == 403
    result = client.get('/api/admin/provider-status', headers=headers(client)).json()
    assert result['successful_generations'] >= 0
    assert result['fallback_generations'] >= 0
    assert 'not a live provider probe' in result['note']


def test_provider_status_records_sanitized_fallback(client):
    class FailingAdapter:
        provider = 'test-provider'

        def generate(self, messages):
            raise RuntimeError('private-provider-secret')

    original = client.app.state.llm
    client.app.state.llm = LLMService(adapter=FailingAdapter())
    try:
        result = client.post('/api/chat', headers=headers(client, 'finance_user'),
                             json={'query': 'executive bonus pool'})
        assert result.status_code == 200
        assert result.json()['generation_mode'] == 'extractive_fallback'
        status = client.get('/api/admin/provider-status', headers=headers(client))
        assert status.json()['fallback_generations'] >= 1
        assert 'private-provider-secret' not in result.text + status.text
    finally:
        client.app.state.llm = original
