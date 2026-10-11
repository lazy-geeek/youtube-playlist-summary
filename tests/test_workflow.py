import json
import sqlite3
import threading
import httpx
import pytest
from cryptography.fernet import Fernet
from app.db import Store, Busy, now
from app.service import Service, probe_config, clean_prose
from app.providers import Temporary, Unavailable, ContextTooLong, Transcripts, OpenRouter, CHAT_SYSTEM


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path,Fernet.generate_key().decode())


def make_run(store, *, read=True):
    c = {'source':'source','archive':'archive','unavailable':'rejects','google_subject':'owner'}
    store.save_settings({'google_subject':'owner'})
    rid = store.execute('INSERT INTO runs(created,status,config,markdown) VALUES (?,?,?,?)',
        (now(),'ready',json.dumps(c),'# Bericht\n\n## Thema\n\nInhalt'))
    store.execute('INSERT INTO sections(run_id,position,title,markdown,read_at) VALUES (?,?,?,?,?)',
        (rid,0,'Thema','Inhalt',now() if read else None))
    for item_id, vid, status, target in [('item-1','video-1','success','archive'),('item-2','video-2','unavailable','rejects'),('item-3','video-3','temporary',None)]:
        store.execute('INSERT INTO items(run_id,source_item,video_id,title,status,target) VALUES (?,?,?,?,?,?)',
            (rid,item_id,vid,vid,status,target))
    return rid


class FakeYT:
    def __init__(self):
        self.source = {'item-1':('source','video-1'),'item-2':('source','video-2'),'item-3':('source','video-3')}
        self.targets = {}
        self.inserts = []
        self.deletes = []
        self.fail_delete = False
        self.ambiguous_insert = False
        self.listing_lags = False
        self.units = 0
    def close(self): pass
    def validate_playlists(self,ids): pass
    def target_item(self,target,video): return None if self.listing_lags else self.targets.get((target,video))
    def target_present(self,item,target,video): return self.targets.get((target,video)) == item
    def source_present(self,item,source,video):
        if item in self.source:
            assert self.source[item] == (source,video)
            return True
        return False
    def insert(self,target,video):
        self.inserts.append((target,video))
        result = 'dest-'+video
        self.targets[target,video] = result
        if self.ambiguous_insert:
            self.ambiguous_insert = False
            raise Temporary('Response lost')
        return result
    def delete_item(self,item):
        if self.fail_delete:
            self.fail_delete = False
            raise Temporary('Delete interrupted')
        self.deletes.append(item)
        del self.source[item]


def test_no_mutation_before_confirmation(store,tmp_path):
    rid = make_run(store)
    yt = FakeYT()
    Service(store,tmp_path,youtube_factory=lambda s:yt).archive(rid)
    assert yt.inserts == yt.deletes == []
    assert len(yt.source) == 3


def test_archive_completes_when_playlist_listing_lags_behind_insert(store,tmp_path):
    rid = make_run(store)
    yt = FakeYT()
    yt.listing_lags = True
    service = Service(store,tmp_path,youtube_factory=lambda s:yt)
    service.confirm(rid)
    service.archive(rid)
    assert yt.deletes == ['item-1','item-2']
    assert store.run(rid)['archive_status'] == 'done'


def test_archive_exact_items_and_keep_temporary(store,tmp_path):
    rid = make_run(store)
    yt = FakeYT()
    service = Service(store,tmp_path,youtube_factory=lambda s:yt)
    service.confirm(rid)
    service.archive(rid)
    assert yt.inserts == [('archive','video-1'),('rejects','video-2')]
    assert yt.deletes == ['item-1','item-2']
    assert 'item-3' in yt.source
    assert store.run(rid)['archive_status'] == 'done'
    service.confirm(rid)
    service.archive(rid)
    assert len(yt.inserts) == 2


def test_resume_after_insert_before_delete(store,tmp_path):
    rid = make_run(store)
    yt = FakeYT()
    yt.fail_delete = True
    service = Service(store,tmp_path,youtube_factory=lambda s:yt)
    service.confirm(rid)
    service.archive(rid)
    assert 'item-1' in yt.source
    assert ('archive','video-1') in yt.targets
    assert store.run(rid)['archive_status'] == 'partial'
    service.confirm(rid)
    service.archive(rid)
    assert yt.inserts.count(('archive','video-1')) == 1
    assert store.run(rid)['archive_status'] == 'done'


def test_ambiguous_insert_reconciled_without_duplicate(store,tmp_path):
    rid = make_run(store)
    yt = FakeYT()
    yt.ambiguous_insert = True
    service = Service(store,tmp_path,youtube_factory=lambda s:yt)
    service.confirm(rid)
    service.archive(rid)
    assert 'item-1' in yt.source
    service.confirm(rid)
    service.archive(rid)
    assert yt.inserts.count(('archive','video-1')) == 1
    assert 'item-1' not in yt.source


def test_ambiguous_missing_destination_never_deletes_or_reinserts(store,tmp_path):
    rid = make_run(store)
    yt = FakeYT()
    store.execute("UPDATE items SET move_status='inserting' WHERE source_item='item-1'")
    service = Service(store,tmp_path,youtube_factory=lambda s:yt)
    service.confirm(rid)
    service.archive(rid)
    assert yt.inserts == yt.deletes == []
    assert 'item-1' in yt.source
    assert store.run(rid)['archive_status'] == 'partial'


def test_unread_sections_allow_archive_without_changing_read_state(store,tmp_path):
    rid = make_run(store,read=False)
    service = Service(store,tmp_path)
    service.confirm(rid)
    assert store.run(rid)['confirmed'] is not None
    assert store.run(rid)['unread'] > 0
    store.release()


def test_single_job_and_restart_recovery(store):
    store.claim('report')
    with pytest.raises(Busy): store.claim('archive')
    store.recover()
    store.claim('archive')


def test_secrets_encrypted_and_read_state_persistent(store):
    store.save_settings({'refresh_token':'secret-refresh-value'})
    assert store.settings()['refresh_token'] == 'secret-refresh-value'
    assert b'secret-refresh-value' not in store.path.read_bytes()
    rid = make_run(store,read=False)
    store.execute('UPDATE sections SET read_at=? WHERE run_id=?',(now(),rid))
    assert store.run(rid)['unread'] == 0


def test_missing_import_is_temporary(tmp_path):
    with pytest.raises(Temporary):
        Transcripts('import',tmp_path).fetch('abcdefghijk')


def test_model_cannot_inject_links():
    # Covers the clean_prose path of summaries and resource names; chat answers are tested separately (CHAT-020/021).
    assert 'https' not in clean_prose('[Fake](https://evil.test) https://evil.test/path <script>bad</script>')


@pytest.mark.parametrize('limit,expected', [('',2), ('1',1)])
def test_complete_run_saves_snapshot_and_sections_without_playlist_mutation(store,tmp_path,monkeypatch,limit,expected):
    monkeypatch.setenv('OPENROUTER_API_KEY','env-only-key')
    config = {'source':'source','archive':'archive','unavailable':'rejects','google_subject':'owner',
              'model':'test/model','search_engine':'auto','transcript_mode':'import','refresh_token':'refresh','max_videos':limit}
    store.save_settings(config)
    store.execute('INSERT INTO probes(created,config,results,success) VALUES (?,?,?,1)',
        (now(),json.dumps(probe_config(config)),'[]'))
    transcript_dir = tmp_path / 'transcripts'
    transcript_dir.mkdir()
    texts = {'abcdefghijk':'belegter Inhalt Alpha ' * 30, 'lmnopqrstuv':'belegter Inhalt Beta ' * 30}
    for vid,text in texts.items():
        (transcript_dir / (vid+'.txt')).write_text(text)
    class YT(FakeYT):
        def entries(self,playlist):
            return [{'source_item':item,'video_id':vid,'title':'KI-Neuigkeiten'} for item,vid in [('item-a','abcdefghijk'),('item-b','lmnopqrstuv')]]
        def metadata(self,vid): return {'title':'KI-Neuigkeiten','description':'','channel_id':'channel'}
        def comments(self,*args): return [],'Keine Kommentare'
    class LLM:
        audit=[]
        def __init__(self,c): assert c['openrouter_key'] == 'env-only-key'
        def close(self): pass
        def summarize_video(self,title,text): return 'Eine Zusammenfassung der wesentlichen Aspekte. ' * 5
        def resources(self,transcript,metadata): return []
    yt = YT()
    service = Service(store,tmp_path,youtube_factory=lambda s:yt,llm_factory=LLM)
    rid,c = service.create()
    service.generate(rid,c)
    result = store.run(rid)
    assert result['status'] == 'ready'
    assert result['counts']['success'] == expected
    assert result['unread'] == expected
    assert len(result['sections']) == expected
    assert all(section['markdown'].startswith('Video-ID: ') for section in result['sections'])
    assert result['sections'][0]['title'] == 'KI-Neuigkeiten'
    assert {i['source_item'] for i in result['items']} == ({'item-a','item-b'} if expected == 2 else {'item-a'})
    assert json.loads(result['config'])['max_videos'] == limit
    assert len(yt.inserts) == expected
    assert result['archive_status'] == 'done'
    assert result['unread'] == expected
    assert len(result['sections']) == expected
    assert all(section['markdown'].startswith('Video-ID: ') for section in result['sections'])
    assert 'env-only-key' not in result['config']
    assert result['read_completed'] is None
    # CHAT-002: every section keeps exactly its own full transcript, saved with the section.
    for section in result['sections']:
        vid = section['markdown'].split('\n',1)[0].removeprefix('Video-ID: ')
        assert store.query('SELECT text FROM transcripts WHERE section_id=?',(section['id'],)) == [{'text':texts[vid]}]
        assert texts[vid] not in section['markdown'] and texts[vid] not in result['markdown']
    assert store.query('SELECT COUNT(*) AS n FROM transcripts') == [{'n':expected}]


def test_search_without_tool_execution_cannot_return_candidate():
    llm = OpenRouter({'model':'fake','openrouter_key':'fake'})
    llm.client.close()
    llm.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200,json={
        'choices':[{'finish_reason':'stop','message':{'content':'{"url":"https://example.com"}'}}],
        'usage':{'server_tool_use':{'web_search_requests':0}}})))
    try:
        with pytest.raises(Temporary): llm.call('Search',search=True)
    finally: llm.close()


def mock_llm(handler, model='fake'):
    llm = OpenRouter({'model':model,'openrouter_key':'fake'})
    llm.client.close()
    llm.client = httpx.Client(transport=httpx.MockTransport(handler))
    return llm


def test_rate_limited_model_call_is_retried(monkeypatch):
    monkeypatch.setattr('app.providers.time.sleep', lambda seconds: None)
    answers = [httpx.Response(429, headers={'retry-after':'1'}),
               httpx.Response(200, json={'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]})]
    bodies = []
    def handler(request):
        bodies.append(request.content)
        return answers.pop(0)
    llm = mock_llm(handler)
    try:
        assert llm.call('Prompt') == {'ok':True} and not answers
        assert len(bodies) == 2 and bodies[0] == bodies[1] and len(llm.audit) == 1
    finally: llm.close()


def test_json_answer_wrapped_in_code_fence_and_prose_is_parsed():
    llm = OpenRouter({'model':'fake','openrouter_key':'fake'})
    llm.client.close()
    llm.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200,json={
        'choices':[{'finish_reason':'stop','message':{'content':'```json\n{"resources":[{"name":"Markitdown"}]}\n```\n\nHinweis: Sponsorenlinks fehlen.'}}]})))
    try:
        assert llm.resources('transcript',{}) == [{'name':'Markitdown'}]
    finally: llm.close()


def test_playlist_entries_sorted_by_video_publication_date():
    from app.providers import YouTube
    yt = YouTube(None)
    yt.pages = lambda *a, **k: [
        {'id':'new','snippet':{'title':'New','publishedAt':'2000-01-01T00:00:00Z'},'contentDetails':{'videoId':'new-video','videoPublishedAt':'2026-01-01T00:00:00Z'}},
        {'id':'unknown','snippet':{'title':'Unknown'},'contentDetails':{'videoId':'unknown'}},
        {'id':'old','snippet':{'title':'Old','publishedAt':'2026-01-01T00:00:00Z'},'contentDetails':{'videoId':'old-video','videoPublishedAt':'2020-01-01T00:00:00Z'}}]
    assert [i['source_item'] for i in yt.entries('source')] == ['old','new','unknown']
    yt.close()


def test_video_section_lists_only_verified_links_named_in_video(store,tmp_path,monkeypatch):
    class YT:
        def metadata(self,vid):
            return {'title':'Tools','channel_id':'channel','description':
                'Code: https://github.com/Owner/Repo/tree/main Tool: https://tool.example/?via=creator&plan=pro Sponsor: https://sponsor.example/deal'}
        def comments(self,*args): return [],''
    class LLM:
        config = {'search_engine':'searxng'}
        def resources(self,transcript,metadata):
            return [{'name':'Repo','url':'https://github.com/Owner/Repo/tree/main'},
                    {'name':'Tool','url':'https://tool.example/?via=creator&plan=pro'},
                    {'name':'Other Tool','url':''}]
        def call(self,prompt,**kwargs): return {'verified':True,'reason':'passt'}
    class Response:
        status_code = 200
        def json(self): return {'full_name':'Owner/Repo','html_url':'https://github.com/Owner/Repo'}
    def get(url,**kwargs):
        assert url == 'https://api.github.com/repos/Owner/Repo'
        return Response()
    def no_search(resource): raise Temporary('Unavailable')
    monkeypatch.setattr('app.providers.httpx.get', get)
    monkeypatch.setattr('app.providers.fetch_page', lambda url: (url + '&ref=abc', 'Tool'))
    monkeypatch.setattr('app.providers.searxng_search', no_search)
    section = Service(store,tmp_path).resource_links(YT(),LLM(),{'video_id':'abcdefghijk'},'Transkript')
    assert section.startswith('\n\n### Links aus dem Video\n\n')
    assert '- [Repo](<https://github.com/Owner/Repo>)' in section
    assert '- [Tool](<https://tool.example/?plan=pro>)' in section
    assert '- Other Tool — URL nicht sicher verifiziert.' in section
    assert 'sponsor.example' not in section


# ---- Chat (Issue #1) ----

OK = {'choices':[{'finish_reason':'stop','message':{'content':'Antwort'}}],'id':'gen-1','model':'m','usage':{'prompt_tokens':5}}
WORDS = 'wort ' * 50


def chat_section(store, markdown='Video-ID: abcdefghijk\n\nZusammenfassung', title='Titel', transcript=None, **settings):
    store.save_settings({'model':'test/model','transcript_mode':'import', **settings})
    rid = store.execute('INSERT INTO runs(created,status,config,markdown) VALUES (?,?,?,?)',(now(),'ready','{"model":"old/model"}','# B'))
    sid = store.execute('INSERT INTO sections(run_id,position,title,markdown) VALUES (?,?,?,?)',(rid,0,title,markdown))
    if transcript is not None:
        store.execute('INSERT INTO transcripts(section_id,text) VALUES (?,?)',(sid,transcript))
    return rid,sid


def chat_llm(answer='Antwort', hook=None):
    made = []
    class LLM:
        def __init__(self,c):
            self.config, self.audit, self.closed, self.args = c, [], 0, None
            made.append(self)
        def close(self): self.closed += 1
        def chat(self,material,turns,question):
            self.args = (material,turns,question)
            if hook: hook(self)
            self.audit.append({'id':'gen-1','model':self.config['model'],'usage':{'prompt_tokens_details':{'cached_tokens':3,'cache_write_tokens':4}}})
            if isinstance(answer,Exception): raise answer
            return answer(question) if callable(answer) else answer
    return LLM, made


def patch_fetch(monkeypatch, fetch):
    calls = []
    class T:
        def __init__(self,mode,directory): self.mode, self.directory = mode, directory
        def fetch(self,vid):
            calls.append((self.mode,self.directory,vid))
            return fetch(vid)
    monkeypatch.setattr('app.service.Transcripts',T)
    return calls


def chat_rows(store, sid=None):
    return store.query('SELECT * FROM chat_messages' + (' WHERE section_id=?' if sid else '') + ' ORDER BY id',(sid,) if sid else ())


@pytest.fixture
def chat_env(monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY','env-key')
    monkeypatch.setenv('OPENROUTER_DECISIONS_MODEL','decision/model')


def test_chat_tables_are_idempotent_and_enforce_constraints(tmp_path):  # CHAT-001
    key = Fernet.generate_key().decode()
    first = Store(tmp_path,key)
    rid,sid = chat_section(first,transcript='Text')
    first.execute("INSERT INTO chat_messages(section_id,role,content,created) VALUES (?,'user','Frage',?)",(sid,now()))
    second = Store(tmp_path,key)
    assert {r['name'] for r in second.query("SELECT name FROM sqlite_master WHERE type='table'")} >= {'transcripts','chat_messages'}
    assert second.query('SELECT text FROM transcripts') == [{'text':'Text'}]
    assert len(chat_rows(second)) == 1 and len(second.run(rid)['sections']) == 1
    for sql,params in [('INSERT INTO transcripts(section_id,text) VALUES (?,?)',(sid,'doppelt')),
                       ('INSERT INTO transcripts(section_id,text) VALUES (?,?)',(999,'Waise')),
                       ('INSERT INTO transcripts(section_id,text) VALUES (?,NULL)',(sid,)),
                       ("INSERT INTO chat_messages(section_id,role,content,created) VALUES (?,'user','x',?)",(999,'t')),
                       ("INSERT INTO chat_messages(section_id,role,content,created) VALUES (?,'user',NULL,'t')",(sid,))]:
        with pytest.raises(sqlite3.IntegrityError): second.execute(sql,params)


class GenFixture:
    """Runs Service.generate with scripted transcripts for several videos."""
    def __init__(self, store, tmp_path, monkeypatch):
        monkeypatch.setenv('OPENROUTER_API_KEY','env-only-key')
        self.store, self.tmp_path = store, tmp_path
        config = {'source':'source','archive':'archive','unavailable':'rejects','google_subject':'owner',
                  'model':'test/model','search_engine':'auto','transcript_mode':'import','refresh_token':'refresh','max_videos':''}
        store.save_settings(config)
        store.execute('INSERT INTO probes(created,config,results,success) VALUES (?,?,?,1)',(now(),json.dumps(probe_config(config)),'[]'))
    def run(self, monkeypatch, outcomes):
        def fetch(vid):
            if isinstance(outcomes[vid],Exception): raise outcomes[vid]
            return outcomes[vid], 'test'
        patch_fetch(monkeypatch,fetch)
        class YT(FakeYT):
            def entries(self,playlist): return [{'source_item':'i-'+v,'video_id':v,'title':'Titel '+v} for v in outcomes]
            def metadata(self,vid): return {'title':'T','description':'','channel_id':'c'}
            def comments(self,*args): return [],''
        class LLM:
            def __init__(self,c): self.audit = []
            def close(self): pass
            def summarize_video(self,title,text): return 'Eine Zusammenfassung der wesentlichen Aspekte. ' * 5
            def resources(self,transcript,metadata): return []
        service = Service(self.store,self.tmp_path,youtube_factory=lambda s:YT(),llm_factory=LLM)
        rid,c = service.create()
        service.generate(rid,c)
        return self.store.run(rid)


def test_failed_videos_leave_no_section_or_transcript(store,tmp_path,monkeypatch):  # CHAT-003
    gen = GenFixture(store,tmp_path,monkeypatch)
    result = gen.run(monkeypatch,{'aaaaaaaaaaa':'Gut ' * 50,'bbbbbbbbbbb':Temporary('später'),
                                  'ccccccccccc':Unavailable('Video ist nur für Kanalmitglieder zugänglich.')})
    assert result['status'] == 'ready' and len(result['sections']) == 1
    assert store.query('SELECT text FROM transcripts') == [{'text':'Gut ' * 50}]


def test_transcript_insert_failure_rolls_back_all_sections(store,tmp_path,monkeypatch):  # CHAT-003
    gen = GenFixture(store,tmp_path,monkeypatch)
    store.execute("CREATE TRIGGER fail_second BEFORE INSERT ON transcripts WHEN (SELECT COUNT(*) FROM transcripts) >= 1 "
                  "BEGIN SELECT RAISE(ABORT,'boom'); END")
    result = gen.run(monkeypatch,{'aaaaaaaaaaa':'Eins ' * 50,'bbbbbbbbbbb':'Zwei ' * 50})
    assert result['status'] == 'failed'
    assert store.query('SELECT COUNT(*) AS n FROM sections') == [{'n':0}]
    assert store.query('SELECT COUNT(*) AS n FROM transcripts') == [{'n':0}]


def test_call_message_order_and_default_request_format():  # CHAT-004
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200,json=OK | {'choices':[{'finish_reason':'stop','message':{'content':'{"a":1}'}}]})
    llm = mock_llm(handler)
    try:
        assert llm.call('Frage') == {'a':1}
        assert set(bodies[0]) == {'model','messages','max_tokens','response_format'} and bodies[0]['max_tokens'] == 9000
        assert [m['role'] for m in bodies[0]['messages']] == ['system','user']
        history = [{'role':'user','content':'h1'},{'role':'assistant','content':'h2'}]
        assert llm.call('Neu',json_output=False,system_prompt='SYS',history=history,max_tokens=4000) == '{"a":1}'
        assert bodies[1]['max_tokens'] == 4000 and 'temperature' not in bodies[1] and 'response_format' not in bodies[1]
        assert bodies[1]['messages'] == [{'role':'system','content':'SYS'},*history,{'role':'user','content':'Neu'}]
    finally: llm.close()


@pytest.mark.parametrize('model,cached',[('anthropic/claude-x',True),('~anthropic/claude-haiku-latest',True),('openai/gpt-x',False)])
def test_chat_request_prefix_and_cache_control(model,cached):  # CHAT-005
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200,json=OK)
    llm = mock_llm(handler,model)
    llm.decide = lambda *a,**k: pytest.fail('chat must not use the decisions endpoint')
    try:
        turns = [{'role':'user','content':'f1'},{'role':'assistant','content':'a1'}]
        assert llm.chat('MATERIAL',[],'Frage 1') == 'Antwort'
        llm.chat('MATERIAL',turns,'Frage 2')
    finally: llm.close()
    for body in bodies:
        assert body['messages'][0] == {'role':'system','content':CHAT_SYSTEM}
        assert body['messages'][1:3] == [{'role':'user','content':'MATERIAL'},{'role':'assistant','content':'Material erhalten.'}]
        assert body['max_tokens'] == 4000 and body['model'] == model
        assert not {'tools','response_format','temperature'} & set(body)
        assert ('cache_control' in body) == cached
        if cached: assert body['cache_control'] == {'type':'ephemeral'}
    assert [m['content'] for m in bodies[1]['messages'][3:]] == ['f1','a1','Frage 2']
    assert bodies[0]['messages'][:3] == bodies[1]['messages'][:3]


@pytest.mark.parametrize('status,text,too_long',[
    (400,'{"error":"context_length_exceeded"}',True), (400,'This model\'s Maximum Context is 8k',True),
    (400,'Context LENGTH too big',True), (400,'bad request',False), (500,'context length',False), (429,'context length',False)])
def test_context_too_long_detection(monkeypatch,status,text,too_long):  # CHAT-006
    monkeypatch.setattr('app.providers.time.sleep', lambda seconds: None)
    llm = mock_llm(lambda request: httpx.Response(status,text=text))
    try:
        with pytest.raises(Temporary) as caught: llm.call('x',json_output=False)
    finally: llm.close()
    assert isinstance(caught.value,ContextTooLong) == too_long and issubclass(ContextTooLong,Temporary)
    assert str(caught.value) == f'OpenRouter-Anfrage fehlgeschlagen ({status}).'


@pytest.mark.parametrize('header,wait',[(None,5),('1',1),('99',30)])
def test_chat_inherits_rate_limit_retry(monkeypatch,header,wait):  # CHAT-007
    sleeps, bodies = [], []
    monkeypatch.setattr('app.providers.time.sleep', sleeps.append)
    def handler(request):
        bodies.append(request.content)
        return httpx.Response(429,headers={'retry-after':header} if header else {})
    llm = mock_llm(handler)
    try:
        with pytest.raises(Temporary,match=r'\(429\)'): llm.chat('M',[],'F')
        assert len(bodies) == 3 and len(set(bodies)) == 1 and sleeps == [wait,wait] and llm.audit == []
        answers = [httpx.Response(429),httpx.Response(200,json=OK)]
        bodies.clear(); sleeps.clear()
        llm.client.close()
        llm.client = httpx.Client(transport=httpx.MockTransport(lambda request: (bodies.append(request.content), answers.pop(0))[1]))
        assert llm.chat('M',[],'F') == 'Antwort'
        assert len(bodies) == 2 and bodies[0] == bodies[1] and sleeps == [5] and len(llm.audit) == 1
    finally: llm.close()


def test_chat_uses_current_setup_model_and_exact_material(store,tmp_path,chat_env,monkeypatch):  # CHAT-008
    rid,sid = chat_section(store,markdown='Video-ID: abcdefghijk\n\nSumme\n\n### Links aus dem Video\n\n- [A](<https://a.org>)',
                           title='Der Titel',transcript='Das ganze Transkript')
    calls = patch_fetch(monkeypatch,lambda vid: pytest.fail('no fetch expected'))
    LLM,made = chat_llm('**Roh**')
    service = Service(store,tmp_path,llm_factory=LLM)
    assert service.chat(rid,sid,'Erste?') == '**Roh**'
    material,turns,question = made[0].args
    assert made[0].config['model'] == 'test/model' and made[0].config['openrouter_key'] == 'env-key'
    assert material == ('Titel: Der Titel\n\nZusammenfassung:\nVideo-ID: abcdefghijk\n\nSumme\n\n### Links aus dem Video\n\n- [A](<https://a.org>)'
                        '\n\nTranskript:\nDas ganze Transkript')
    assert 'env-key' not in material and turns == [] and question == 'Erste?'
    store.save_settings({'model':'newer/model'})
    service.chat(rid,sid,'Zweite?')
    assert made[1].config['model'] == 'newer/model'
    assert made[1].args[1] == [{'role':'user','content':'Erste?'},{'role':'assistant','content':'**Roh**'}]
    assert calls == []


def test_chat_unknown_section_and_missing_configuration(store,tmp_path,chat_env,monkeypatch):  # CHAT-009
    rid,sid = chat_section(store)
    other,_ = chat_section(store)
    calls = patch_fetch(monkeypatch,lambda vid: ('Text ' * 50,'x'))
    LLM,made = chat_llm()
    service = Service(store,tmp_path,llm_factory=LLM)
    for args in [(rid,999),(other,sid),(999,sid)]:
        with pytest.raises(KeyError): service.chat(*args,'Frage')
    store.execute("DELETE FROM settings WHERE key='model'")
    with pytest.raises(Temporary,match='Für den Chat zuerst im Setup ein Modell speichern und OPENROUTER_API_KEY setzen.'):
        service.chat(rid,sid,'Frage')
    store.save_settings({'model':'test/model'})
    monkeypatch.delenv('OPENROUTER_API_KEY')
    with pytest.raises(Temporary,match='OPENROUTER_API_KEY setzen'): service.chat(rid,sid,'Frage')
    assert calls == [] and made == [] and chat_rows(store) == [] and store.query('SELECT * FROM transcripts') == []


@pytest.mark.parametrize('mode',['import','public','apify'])
def test_chat_reloads_missing_transcript_once(store,tmp_path,chat_env,monkeypatch,mode):  # CHAT-010
    rid,sid = chat_section(store,transcript_mode=mode)
    calls = patch_fetch(monkeypatch,lambda vid: ('Nachgeladen ' * 50,'o'))
    LLM,made = chat_llm()
    service = Service(store,tmp_path,llm_factory=LLM)
    service.chat(rid,sid,'Eins')
    assert calls == [(mode,tmp_path,'abcdefghijk')]
    assert store.query('SELECT text FROM transcripts') == [{'text':'Nachgeladen ' * 50}]
    store.save_settings({'transcript_mode':'public' if mode != 'public' else 'import'})
    patch_fetch(monkeypatch,lambda vid: pytest.fail('second fetch'))
    service.chat(rid,sid,'Zwei')
    assert made[1].args[0].endswith('Transkript:\n' + 'Nachgeladen ' * 50)
    assert store.query('SELECT text FROM transcripts') == [{'text':'Nachgeladen ' * 50}]


def test_chat_without_video_prefix_or_with_source_failure(store,tmp_path,chat_env,monkeypatch):  # CHAT-011
    LLM,_ = chat_llm()
    service = Service(store,tmp_path,llm_factory=LLM)
    rid,sid = chat_section(store,markdown='kein Präfix')
    calls = patch_fetch(monkeypatch,lambda vid: pytest.fail('no fetch'))
    with pytest.raises(Temporary,match='Zu diesem Abschnitt ist kein Video hinterlegt; ein Chat ist nicht möglich.'):
        service.chat(rid,sid,'F')
    assert calls == []
    store.execute('INSERT INTO transcripts(section_id,text) VALUES (?,?)',(sid,'Gespeichert'))
    assert service.chat(rid,sid,'F') == 'Antwort'
    rid,sid = chat_section(store)
    monkeypatch.undo()
    monkeypatch.setenv('OPENROUTER_API_KEY','env-key')
    with pytest.raises(Temporary,match='^Transkript konnte nicht nachgeladen werden: Kein autorisiert importiertes Transkript vorhanden.$'):
        service.chat(rid,sid,'F')
    for error in [Temporary('Quelle gesperrt'),Unavailable('Video ist nur für Kanalmitglieder zugänglich.')]:
        patch_fetch(monkeypatch,lambda vid,error=error: (_ for _ in ()).throw(error))
        with pytest.raises(Temporary,match='^Transkript konnte nicht nachgeladen werden: ' + str(error)): service.chat(rid,sid,'F')
    assert store.query('SELECT section_id FROM transcripts') != [] and len(chat_rows(store)) == 2
    assert store.query('SELECT * FROM transcripts WHERE section_id=?',(sid,)) == []
    patch_fetch(monkeypatch,lambda vid: ('Jetzt da ' * 50,'o'))
    assert service.chat(rid,sid,'F') == 'Antwort'


@pytest.mark.parametrize('pairs,sent',[(0,0),(1,2),(10,20),(11,20)])
def test_chat_sends_only_last_ten_pairs_of_own_section(store,tmp_path,chat_env,pairs,sent):  # CHAT-012
    rid,sid = chat_section(store,transcript='T')
    _,foreign = chat_section(store,transcript='Fremd')
    for n in range(pairs):
        for target,tag in [(sid,''),(foreign,'fremd-')]:
            for role in ('user','assistant'):
                store.execute('INSERT INTO chat_messages(section_id,role,content,created) VALUES (?,?,?,?)',(target,role,f'{tag}{role}-{n}',now()))
    LLM,made = chat_llm()
    Service(store,tmp_path,llm_factory=LLM).chat(rid,sid,'Neu')
    turns = made[0].args[1]
    assert len(turns) == sent and all(not t['content'].startswith('fremd') for t in turns)
    if turns:
        assert turns[0]['role'] == 'user' and [t['role'] for t in turns] == ['user','assistant'] * (sent // 2)
        assert turns[-1]['content'] == f'assistant-{pairs - 1}' and turns[0]['content'] == f'user-{pairs - sent // 2}'
    assert len(chat_rows(store,sid)) == pairs * 2 + 2
    assert made[0].args[0] == 'Titel: Titel\n\nZusammenfassung:\nVideo-ID: abcdefghijk\n\nZusammenfassung\n\nTranskript:\nT'


def test_chat_persists_trimmed_raw_answer_and_audit(store,tmp_path,chat_env):  # CHAT-013
    rid,sid = chat_section(store,transcript='T')
    LLM,made = chat_llm('  **Hallo** [Repo](https://github.com/o/r)\n')
    assert Service(store,tmp_path,llm_factory=LLM).chat(rid,sid,'Wie?\nZeile') == '**Hallo** [Repo](https://github.com/o/r)'
    with sqlite3.connect(store.path) as db:
        rows = db.execute('SELECT id,role,content,created,audit FROM chat_messages ORDER BY id').fetchall()
    assert [r[1] for r in rows] == ['user','assistant'] and rows[1][0] == rows[0][0] + 1
    assert rows[0][2] == 'Wie?\nZeile' and rows[1][2] == '**Hallo** [Repo](https://github.com/o/r)'
    assert rows[0][3] and rows[1][3] and rows[0][4] is None
    assert json.loads(rows[1][4]) == made[0].audit and len(made) == 1 and made[0].closed == 1
    assert json.loads(rows[1][4])[0]['usage']['prompt_tokens_details']['cache_write_tokens'] == 4


@pytest.mark.parametrize('answer,message',[
    (None,'Das Modell hat keine verwertbare Antwort geliefert. Bitte erneut fragen.'),
    (42,'Das Modell hat keine verwertbare Antwort geliefert. Bitte erneut fragen.'),
    ('','Das Modell hat keine verwertbare Antwort geliefert. Bitte erneut fragen.'),
    (' \n\t ','Das Modell hat keine verwertbare Antwort geliefert. Bitte erneut fragen.'),
    (ContextTooLong('x'),'Das Video ist zu lang für das Kontextfenster des eingestellten Modells. Im Setup ein Modell mit größerem Kontextfenster wählen.'),
    (Temporary('Modellausgabe wurde abgebrochen oder abgeschnitten.'),'Modellausgabe wurde abgebrochen oder abgeschnitten.'),
    (RuntimeError('kaputt'),None), ('Antwort','INSERT')])
def test_chat_failures_store_nothing_and_close_llm(store,tmp_path,chat_env,monkeypatch,answer,message):  # CHAT-014
    rid,sid = chat_section(store)
    patch_fetch(monkeypatch,lambda vid: ('Neu ' * 50,'o'))
    if message == 'INSERT':
        store.execute("CREATE TRIGGER no_answer BEFORE INSERT ON chat_messages WHEN NEW.role='assistant' BEGIN SELECT RAISE(ABORT,'boom'); END")
    LLM,made = chat_llm(answer)
    service = Service(store,tmp_path,llm_factory=LLM)
    expected = sqlite3.IntegrityError if message == 'INSERT' else RuntimeError if message is None else Temporary
    with pytest.raises(expected) as caught: service.chat(rid,sid,'Frage')
    if message not in (None,'INSERT'): assert str(caught.value) == message
    assert chat_rows(store) == [] and len(made) == 1 and made[0].closed == 1
    assert store.query('SELECT text FROM transcripts') == [{'text':'Neu ' * 50}]


def test_concurrent_first_questions_share_one_stored_transcript(store,tmp_path,chat_env,monkeypatch):  # CHAT-015
    rid,sid = chat_section(store)
    barrier, lock, counter = threading.Barrier(2), threading.Lock(), []
    def fetch(vid):
        barrier.wait(10)
        with lock:
            counter.append(1)
            return f'Quelle {len(counter)} ' * 50, 'o'
    patch_fetch(monkeypatch,fetch)
    second_returned = threading.Event()
    def hook(llm):
        if llm.args[2] == 'q2': second_returned.set()
        else: assert second_returned.wait(10)
    LLM,made = chat_llm(lambda q: 'A-' + q,hook)
    service = Service(store,tmp_path,llm_factory=LLM)
    errors = []
    def ask(q):
        try: service.chat(rid,sid,q)
        except Exception as exc: errors.append(exc)
    threads = [threading.Thread(target=ask,args=(q,)) for q in ('q1','q2')]
    for t in threads: t.start()
    for t in threads: t.join(20)
    assert errors == [] and len(counter) == 2
    stored = store.query('SELECT text FROM transcripts')
    assert len(stored) == 1 and all(llm.args[0].endswith(stored[0]['text']) for llm in made)
    rows = chat_rows(store)
    assert [r['role'] for r in rows] == ['user','assistant'] * 2
    for user,assistant in zip(rows[::2],rows[1::2]): assert assistant['content'] == 'A-' + user['content']


def test_chat_system_prompt_keywords():  # CHAT-024
    for word in ('Deutsch','ausschließlich','untrusted','Anweisungen','erfinde nichts','wörtlich','URL','Transkript','Zusammenfassung'):
        assert word in CHAT_SYSTEM
    assert 'Datum' not in CHAT_SYSTEM


def test_chat_without_configured_transcript_source(store,tmp_path,chat_env):  # CHAT-038
    rid,sid = chat_section(store)
    store.execute("DELETE FROM settings WHERE key='transcript_mode'")
    LLM,made = chat_llm()
    with pytest.raises(Temporary) as caught: Service(store,tmp_path,llm_factory=LLM).chat(rid,sid,'F')
    assert str(caught.value) == 'Transkript konnte nicht nachgeladen werden: Transkriptquelle noch nicht konfiguriert.'
    assert store.query('SELECT * FROM transcripts') == [] and chat_rows(store) == [] and made == []
