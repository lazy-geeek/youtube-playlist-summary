import json
import pytest
from cryptography.fernet import Fernet
from app.db import Store, Busy, now
from app.service import Service, probe_config, clean_prose
from app.providers import Temporary, Transcripts, OpenRouter


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
        self.units = 0
    def close(self): pass
    def validate_playlists(self,ids): pass
    def target_item(self,target,video): return self.targets.get((target,video))
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
    for vid in ['abcdefghijk','lmnopqrstuv']:
        (transcript_dir / (vid+'.txt')).write_text('belegter Inhalt ' * 30)
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


def test_search_without_tool_execution_cannot_return_candidate():
    import httpx
    llm = OpenRouter({'model':'fake','openrouter_key':'fake'})
    llm.client.close()
    llm.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200,json={
        'choices':[{'finish_reason':'stop','message':{'content':'{"url":"https://example.com"}'}}],
        'usage':{'server_tool_use':{'web_search_requests':0}}})))
    try:
        with pytest.raises(Temporary): llm.call('Search',search=True)
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
