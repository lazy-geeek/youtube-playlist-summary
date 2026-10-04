import pytest
from app.providers import Links, Temporary, searxng_search


def test_searxng_json_query(monkeypatch):
    monkeypatch.setenv('SEARXNG_URL', 'http://search.internal:8080/')
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'results':[{'url':'https://example.com/project','title':'Project','content':'Text'}]}
    def get(url, **kwargs):
        assert url == 'http://search.internal:8080/search'
        assert kwargs['params']['format'] == 'json'
        assert 'Project' in kwargs['params']['q']
        return Response()
    monkeypatch.setattr('app.providers.httpx.get', get)
    assert searxng_search({'name':'Project'})[0]['url'] == 'https://example.com/project'


def test_searxng_missing_config(monkeypatch):
    monkeypatch.delenv('SEARXNG_URL', raising=False)
    with pytest.raises(Temporary, match='SEARXNG_URL'):
        searxng_search({'name':'Project'})


def test_searxng_rejects_invented_url_without_paid_search(monkeypatch):
    monkeypatch.setattr('app.providers.searxng_search', lambda resource: [{'url':'https://example.com/real'}])
    class LLM:
        config = {'search_engine':'searxng'}
        def call(self, prompt, **kwargs):
            assert not kwargs.get('search')
            return {'url':'https://example.com/invented'}
    result = Links(LLM()).verify({'name':'Project'}, set())
    assert not result['verified']
    assert result['url'] == ''


def test_searxng_failure_does_not_call_openrouter_search(monkeypatch):
    def fail(resource): raise Temporary('Unavailable')
    monkeypatch.setattr('app.providers.searxng_search', fail)
    class LLM:
        config = {'search_engine':'searxng'}
        def call(self, *args, **kwargs): raise AssertionError('Must not fall back')
    assert not Links(LLM()).verify({'name':'Project'}, set())['verified']
