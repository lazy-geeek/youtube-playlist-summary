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


def test_model_proposed_github_repo_is_verified_without_search(monkeypatch):
    class Response:
        status_code = 200
        def json(self): return {'full_name':'Owner/Repo','html_url':'https://github.com/Owner/Repo'}
    monkeypatch.setattr('app.providers.httpx.get', lambda url, **kwargs: Response())
    def no_search(resource): raise AssertionError('Must not search')
    monkeypatch.setattr('app.providers.searxng_search', no_search)
    class LLM:
        config = {'search_engine':'searxng'}
        def call(self, prompt, **kwargs): return {'verified':True}
    result = Links(LLM()).verify({'name':'Repo','url':'https://github.com/Owner/Repo'}, set())
    assert result['verified'] and result['url'] == 'https://github.com/Owner/Repo' and not result['searched']


def test_model_proposed_github_repo_that_does_not_exist_stays_unverified(monkeypatch):
    class Response:
        status_code = 404
    monkeypatch.setattr('app.providers.httpx.get', lambda url, **kwargs: Response())
    monkeypatch.setattr('app.providers.searxng_search', lambda resource: [])
    class LLM:
        config = {'search_engine':'searxng'}
        def call(self, prompt, **kwargs): return {'url':''}
    result = Links(LLM()).verify({'name':'Repo','url':'https://github.com/Owner/Invented'}, set())
    assert not result['verified'] and result['url'] == '' and result['searched']


def test_searxng_prefers_model_written_query(monkeypatch):
    monkeypatch.setenv('SEARXNG_URL', 'http://search.internal:8080')
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'results':[]}
    def get(url, **kwargs):
        assert kwargs['params']['q'] == 'AI Explained YouTube channel'
        return Response()
    monkeypatch.setattr('app.providers.httpx.get', get)
    assert searxng_search({'name':'AI Explained','query':'AI Explained YouTube channel'}) == []


def test_youtube_channel_is_verified_through_data_api(monkeypatch):
    def no_fetch(url): raise AssertionError('Must not fetch the consent stub')
    monkeypatch.setattr('app.providers.fetch_page', no_fetch)
    monkeypatch.setattr('app.providers.searxng_search', lambda resource: [{'url':'https://www.youtube.com/@Fireship/videos'}])
    class YT:
        def request(self, method, resource, params=None):
            assert (resource, params['forHandle']) == ('channels', '@Fireship')
            return {'items':[{'id':'UCsBjURrPoezykLs9EqgamOA','snippet':{'title':'Fireship','customUrl':'@fireship'}}]}
    class LLM:
        config = {'search_engine':'searxng'}
        def call(self, prompt, **kwargs):
            return {'url':'https://www.youtube.com/@Fireship/videos'} if prompt.startswith('Wähle') else {'verified':True}
    result = Links(LLM(), YT()).verify({'name':'Fireship'}, set())
    assert result['verified'] and result['url'] == 'https://www.youtube.com/@fireship'


def test_blocked_page_falls_back_to_search_entry(monkeypatch):
    def blocked(url): raise ValueError('Ziel nicht abrufbar.')
    monkeypatch.setattr('app.providers.fetch_page', blocked)
    monkeypatch.setattr('app.providers.searxng_search', lambda resource: [
        {'url':'https://www.producthunt.com/','title':'Product Hunt','content':'The best new products in tech.'}])
    prompts = []
    class LLM:
        config = {'search_engine':'searxng'}
        def call(self, prompt, **kwargs):
            prompts.append(prompt)
            return {'url':'https://www.producthunt.com/'} if prompt.startswith('Wähle') else {'verified':True}
    result = Links(LLM()).verify({'name':'Product Hunt'}, set())
    assert result['verified'] and result['url'] == 'https://www.producthunt.com/'
    assert 'Seite nicht abrufbar' in prompts[-1] and 'The best new products in tech.' in prompts[-1]
    # A blocked address from the video data has no independent search entry and stays unverified.
    assert not Links(LLM()).check({'name':'Product Hunt'}, 'https://www.producthunt.com/')


def test_x_links_collapse_to_profile():
    from app.service import clean_url
    assert clean_url('https://x.com/bcherny/status/2007179832300581177') == 'https://x.com/bcherny'
    assert clean_url('https://x.com/ylecun?lang=') == 'https://x.com/ylecun'
    assert clean_url('https://x.com/i/lists/123') == 'https://x.com/i/lists/123'


def test_identity_uses_decision_endpoint_and_falls_back_to_model_verdict(monkeypatch):
    monkeypatch.setattr('app.providers.fetch_page', lambda url: (url, 'Project page'))
    class Decider:
        config = {}
        def decide(self, state, instructions, criteria):
            assert state['candidate'] == 'https://example.com/project' and set(criteria) == {'same','different','unclear'}
            return {'same':self.same,'different':1 - self.same}
        def call(self, prompt, **kwargs): raise AssertionError('Must not ask the text model')
    llm = Decider()
    llm.same = .9
    assert Links(llm).check({'name':'Project'}, 'https://example.com/project')['verified']
    llm.same = .1
    assert not Links(llm).check({'name':'Project'}, 'https://example.com/project')
    class Borderline(Decider):
        same = .5
        def call(self, prompt, **kwargs): return {'verified':True,'reason':'Name matches'}
    assert Links(Borderline()).check({'name':'Project'}, 'https://example.com/project')['reason'] == 'Name matches'
    class Unavailable(Borderline):
        def decide(self, *args): raise Temporary('Alpha endpoint changed')
    assert Links(Unavailable()).check({'name':'Project'}, 'https://example.com/project')['reason'] == 'Name matches'


def test_decide_is_on_by_default_and_can_be_switched_off(monkeypatch):
    import httpx
    from app.providers import OpenRouter
    llm = OpenRouter({'model':'fake','openrouter_key':'fake'})
    llm.client.close()
    def answer(request):
        assert request.url.path == '/api/alpha/decisions'
        return httpx.Response(200, json={'model':'decider','answers':{'decision':{'choice':'same','probabilities':{'same':.9}}}})
    llm.client = httpx.Client(transport=httpx.MockTransport(answer))
    try:
        monkeypatch.delenv('OPENROUTER_DECISIONS_MODEL', raising=False)
        assert llm.decide({}, 'Decide', {'same':'Same'}) == {'same':.9} and llm.audit[-1]['model'] == 'decider'
        monkeypatch.setenv('OPENROUTER_DECISIONS_MODEL', '')
        with pytest.raises(Temporary): llm.decide({}, 'Decide', {'same':'Same'})
    finally: llm.close()


def test_page_text_keeps_title_meta_and_structured_data_but_drops_scripts_and_styles():
    from app.providers import page_text
    text = page_text('<html><head><title>Jim Fan (@DrJimFan) / X</title><style>:root{color:#fff}</style>'
        '<meta content="NVIDIA Director of Robotics" property="og:description"><script>var tracking = 1</script>'
        '<script type="application/ld+json">{"name":"Jim Fan"}</script></head><body><p>Posts &amp; replies</p></body></html>')
    assert text.startswith('NVIDIA Director of Robotics') and 'Jim Fan (@DrJimFan) / X' in text
    assert '{"name":"Jim Fan"}' in text and 'Posts & replies' in text
    assert 'tracking' not in text and ':root' not in text


def test_search_hit_is_picked_by_decision_model_with_text_model_fallback(monkeypatch):
    hits = [{'url':'https://other.example/','title':'Other','content':''},
            {'url':'https://project.example/','title':'Project','content':'Official site'}]
    monkeypatch.setattr('app.providers.searxng_search', lambda resource: hits)
    class Decider:
        config = {'search_engine':'searxng'}
        chances = {'1':.1,'2':.8,'none':.1}
        def decide(self, state, instructions, criteria):
            assert criteria['2'] == 'https://project.example/' and 'none' in criteria
            return self.chances
        def call(self, prompt, **kwargs): raise AssertionError('Must not ask the text model')
    llm = Decider()
    assert Links(llm).search({'name':'Project'}) == ('https://project.example/', 'Project\nOfficial site')
    llm.chances = {'1':.1,'2':.2,'none':.7}
    assert Links(llm).search({'name':'Project'}) == ('', '')
    class Unavailable(Decider):
        def decide(self, *args): raise Temporary('Alpha endpoint changed')
        def call(self, prompt, **kwargs): return {'url':'https://other.example/'}
    assert Links(Unavailable()).search({'name':'Project'})[0] == 'https://other.example/'
