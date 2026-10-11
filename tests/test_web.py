import base64
import importlib
import re
import shutil
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from app.providers import Temporary


@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setenv('APP_PASSWORD','long-password-for-testing')
    monkeypatch.setenv('APP_USERNAME','reader')
    monkeypatch.setenv('ENCRYPTION_KEY',Fernet.generate_key().decode())
    monkeypatch.setenv('DATA_DIR',str(tmp_path))
    monkeypatch.setenv('APP_URL','http://localhost:8765')
    import app.main
    main = importlib.reload(app.main)
    with TestClient(main.app,base_url='http://localhost:8765') as c:
        yield c


def authorize(c):
    c.headers['Authorization'] = 'Basic '+base64.b64encode(b'reader:long-password-for-testing').decode()


def csrf(c):
    page = c.get('/').text
    return re.search('name="csrf" value="([^"]+)"',page).group(1)


def test_all_routes_and_exports_protected(client):
    for url in ['/','/setup','/history','/runs/1/markdown','/health','/static/app.js']:
        assert client.get(url).status_code == 401
    assert client.post('/runs/1/sections/1/chat').status_code == 401
    assert client.post('/runs/1/sections/1/chat',data={'csrf':'x','question':'Hallo'},headers={'Origin':'http://localhost:8765'}).status_code == 401


def test_setup_and_home_render_without_secrets(client):
    authorize(client)
    client.app.state.store.save_settings({'google_client_secret':'secret-google','refresh_token':'secret-refresh'})
    for url in ['/','/setup','/history']:
        response = client.get(url)
        assert response.status_code == 200
        assert 'secret-google' not in response.text and 'secret-refresh' not in response.text
        assert 'OPENROUTER_API_KEY' in client.get('/setup').text


def test_csrf_and_origin_required(client):
    authorize(client)
    token = csrf(client)
    assert client.post('/runs',data={'csrf':token}).status_code == 403
    assert client.post('/runs',data={'csrf':'bad'},headers={'Origin':'http://localhost:8765'}).status_code == 403
    chat = '/runs/1/sections/1/chat'
    assert client.post(chat,data={'csrf':token,'question':'Hallo'}).status_code == 403
    assert client.post(chat,data={'csrf':token,'question':'Hallo'},headers={'Origin':'http://evil.example'}).status_code == 403
    assert client.post(chat,data={'csrf':'bad','question':'Hallo'},headers={'Origin':'http://localhost:8765'}).status_code == 403
    assert client.post(chat,data={'question':'Hallo'},headers={'Origin':'http://localhost:8765'}).status_code == 403


def test_section_disappears_persists_and_export_unchanged(client):
    authorize(client)
    s = client.app.state.store
    rid = s.execute('INSERT INTO runs(created,status,config,markdown) VALUES (?,?,?,?)',('2026-10-04','ready','{}','# Bericht\n\n## KI\n\nDetails'))
    sec = s.execute('INSERT INTO sections(run_id,position,title,markdown) VALUES (?,?,?,?)',(rid,0,'KI','Video-ID: abcdefghijk\n\nDetails'))
    s.execute('INSERT INTO items(run_id,source_item,video_id,title) VALUES (?,?,?,?)',(rid,'i1','abcdefghijk','KI'))
    s.execute('INSERT INTO transcripts(section_id,text) VALUES (?,?)',(sec,'Das Transkript'))
    s.execute("INSERT INTO chat_messages(section_id,role,content,created) VALUES (?,'user','Eine Frage',?)",(sec,'t'))
    s.execute("INSERT INTO chat_messages(section_id,role,content,created) VALUES (?,'assistant','Eine **Antwort**',?)",(sec,'t'))
    saved = s.query('SELECT * FROM chat_messages')
    token = csrf(client)
    page = client.get(f'/runs/{rid}').text
    assert '1 von 1 Abschnitten noch offen' in page
    assert page.count('class="chat"') == 1 and 'Eine Frage' in page and page.index('class="reading-section"') < page.index('class="chat"')
    r = client.post(f'/runs/{rid}/sections/{sec}/read',data={'csrf':token},headers={'Origin':'http://localhost:8765'})
    assert r.status_code == 200
    assert '0 von 1 Abschnitten noch offen' in r.text
    assert 'Gelesene Abschnitte (1)' in r.text
    assert s.run(rid)['read_completed'] is not None
    assert 'reading-section' not in r.text
    # CHAT-023: the chat stays reachable in the read group and is not part of the export.
    assert r.text.count('class="chat"') == 1 and r.text.index('read-sections') < r.text.index('class="chat"')
    assert '<strong>Antwort</strong>' in r.text
    assert 'Eine Frage' not in s.run(rid)['markdown'] and 'Antwort' not in client.get(f'/runs/{rid}/markdown').text
    assert client.get(f'/runs/{rid}/markdown').text.endswith('Details')
    client.post(f'/runs/{rid}/sections/{sec}/read',data={'csrf':token,'unread':'1'},headers={'Origin':'http://localhost:8765'})
    assert s.run(rid)['unread'] == 1
    assert s.run(rid)['read_completed'] is None
    again = client.get(f'/runs/{rid}').text
    assert again.count('class="chat"') == 1 and again.index('class="reading-section"') < again.index('class="chat"')
    assert s.query('SELECT * FROM chat_messages') == saved and s.query('SELECT text FROM transcripts') == [{'text':'Das Transkript'}]


def test_oauth_callback_without_state_rejected(client):
    authorize(client)
    assert 'ungültig' in client.get('/oauth/callback?state=wrong&code=fake').text


def test_report_html_safe(client):
    authorize(client)
    s = client.app.state.store
    rid = s.execute('INSERT INTO runs(created,status,config,markdown) VALUES (?,?,?,?)',('2026','ready','{}','# Safe <script>alert(1)</script>'))
    assert '<script>alert(1)</script>' not in client.get(f'/runs/{rid}').text
    sec = s.execute('INSERT INTO sections(run_id,position,title,markdown) VALUES (?,?,?,?)',(rid,0,'T','Video-ID: abcdefghijk\n\nText'))
    s.execute('INSERT INTO items(run_id,source_item,video_id,title) VALUES (?,?,?,?)',(rid,'i1','abcdefghijk','T'))
    s.execute("INSERT INTO chat_messages(section_id,role,content,created) VALUES (?,'user','<script>alert(2)</script> <img src=x onerror=alert(3)>',?)",(sec,'t'))
    s.execute("INSERT INTO chat_messages(section_id,role,content,created) VALUES (?,'assistant',?,?)",
              (sec,'<script>alert(4)</script> <a href="https://x.org" onclick="y()">k</a> ![b](https://x.org/i.png) [j](javascript:alert(5)) [h](http://x.org)','t'))
    text = client.get(f'/runs/{rid}').text
    chat = text.split('<details class="chat">',1)[1].split('</details>',1)[0]
    assert '&lt;script&gt;alert(2)' in chat and '<script' not in chat and '<img' not in chat and not re.search(r'<[^>]*\bon\w+=',chat)
    anchors = re.findall(r'<a [^>]*>',chat)
    assert len(anchors) == 1 and 'href="https://x.org"' in anchors[0] and 'rel="noopener noreferrer"' in anchors[0]
    assert '>h</a>' not in chat and '>j</a>' not in chat



def test_count_includes_all_current_playlist_entries(client,monkeypatch):
    authorize(client)
    s = client.app.state.store
    s.save_settings({'source':'source','refresh_token':'token'})
    class YT:
        def __init__(self,store): pass
        def entries(self,source): return [{'source_item':'current-1'},{'source_item':'current-2'}]
        def close(self): pass
    import app.main
    monkeypatch.setattr(app.main,'YouTube',YT)
    assert client.get('/api/count').json() == {'total':2,'pending':2,'held':0}


def test_partial_setup_saves_and_preserves_secret(client):
    authorize(client)
    s = client.app.state.store
    s.save_settings({'google_client_secret':'saved-secret'})
    fields = {'csrf':csrf(client), 'source':'PLabcdefghijk', 'google_client_id':'my-client',
              'search_engine':'auto', 'transcript_mode':'import'}
    response = client.post('/setup',data=fields,headers={'Origin':'http://localhost:8765'})
    assert response.status_code == 200
    assert s.settings()['source'] == 'PLabcdefghijk'
    assert s.settings()['google_client_id'] == 'my-client'
    assert s.settings()['google_client_secret'] == 'saved-secret'
    assert 'input required' not in response.text
    assert 'Google-Zugang' in client.post('/oauth/start',data={'csrf':csrf(client)},headers={'Origin':'http://localhost:8765'}).text


def test_partial_setup_saves_draft_playlist(client):
    authorize(client)
    response = client.post('/setup',data={'csrf':csrf(client), 'source':'bad',
        'search_engine':'auto', 'transcript_mode':'import'},headers={'Origin':'http://localhost:8765'})
    assert response.status_code == 200
    assert client.app.state.store.settings()['source'] == 'bad'


def test_setup_extracts_playlist_id_from_url(client):
    authorize(client)
    response = client.post('/setup',data={'csrf':csrf(client),
        'source':'https://www.youtube.com/playlist?list=PLabcdefghijk&index=1',
        'search_engine':'auto', 'transcript_mode':'import'},headers={'Origin':'http://localhost:8765'})
    assert response.status_code == 200
    assert client.app.state.store.settings()['source'] == 'PLabcdefghijk'


def test_google_oauth_redirect_allowed_by_csp(client):
    authorize(client)
    client.app.state.store.save_settings({'google_client_id':'client', 'google_client_secret':'secret',
                                          'google_allowed_email':'reader@example.com'})
    page = client.get('/setup')
    assert "form-action 'self' https://accounts.google.com" in page.headers['content-security-policy']
    response = client.post('/oauth/start', data={'csrf':csrf(client)},
        headers={'Origin':'http://localhost:8765'}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'].startswith('https://accounts.google.com/o/oauth2/v2/auth?')
    assert "form-action 'self' https://accounts.google.com" in response.headers['content-security-policy']


def test_authentication_remembered_by_cookie(client):
    authorize(client)
    response = client.get('/')
    cookie = response.headers['set-cookie']
    assert 'Max-Age=2592000' in cookie and 'HttpOnly' in cookie
    client.headers.pop('Authorization')
    assert client.get('/setup').status_code == 200
    client.cookies.clear()
    assert client.get('/setup').status_code == 401


def test_password_change_invalidates_remembered_session(client, monkeypatch):
    authorize(client)
    assert client.get('/').status_code == 200
    client.headers.pop('Authorization')
    monkeypatch.setenv('APP_PASSWORD','another-long-password')
    assert client.get('/').status_code == 401


def test_section_renders_markdown_table(client):
    authorize(client)
    s = client.app.state.store
    rid = s.execute('INSERT INTO runs(created,status,config,markdown) VALUES (?,?,?,?)',('2026-10-04','ready','{}','# Bericht'))
    s.execute('INSERT INTO sections(run_id,position,title,markdown) VALUES (?,?,?,?)',
        (rid,0,'KI','**Testergebnis** (alle Stufen):\n| Stufe | Dauer |\n|---|---|\n| Low | 27 Sekunden |'))
    text = client.get(f'/runs/{rid}').text
    assert '<th>Stufe</th>' in text and '<td>27 Sekunden</td>' in text
    assert '|---|' not in text


# ---- Chat (Issue #1) ----

ORIGIN = {'Origin':'http://localhost:8765'}


def video_section(s, markdown='Video-ID: abcdefghijk\n\nZusammenfassung', video='abcdefghijk', title='Titel', position=0, rid=None, transcript=None):
    rid = rid or s.execute('INSERT INTO runs(created,status,config,markdown) VALUES (?,?,?,?)',('2026-10-04','ready','{}','# Bericht'))
    sid = s.execute('INSERT INTO sections(run_id,position,title,markdown) VALUES (?,?,?,?)',(rid,position,title,markdown))
    if video:
        s.execute('INSERT INTO items(run_id,source_item,video_id,title) VALUES (?,?,?,?)',(rid,f'i{sid}',video,title))
    if transcript:
        s.execute('INSERT INTO transcripts(section_id,text) VALUES (?,?)',(sid,transcript))
    return rid,sid


def message(s, sid, role, content):
    s.execute('INSERT INTO chat_messages(section_id,role,content,created) VALUES (?,?,?,?)',(sid,role,content,'t'))


def anchors(html):
    return [(re.search(r'href="([^"]*)"',a).group(1) if 'href=' in a else None, a) for a in re.findall(r'<a [^>]*>|<a>',html)]


def post_chat(client, rid, sid, **data):
    return client.post(f'/runs/{rid}/sections/{sid}/chat',data={'csrf':csrf(client),**data},headers=ORIGIN)


def test_chat_route_contract_and_error_mapping(client):  # CHAT-018
    authorize(client)
    quiet = TestClient(client.app,base_url='http://localhost:8765',raise_server_exceptions=False)
    quiet.headers['Authorization'] = client.headers['Authorization']
    service = client.app.state.service
    outcomes = {'ok':'Antwort mit [L](https://example.org/x)','key':KeyError(1),'temp':Temporary('Meldung aus Temporary'),'boom':RuntimeError('x')}
    current = {}
    def fake(run_id, section_id, question):
        result = outcomes[current['k']]
        if isinstance(result,Exception): raise result
        return result
    service.chat = fake
    current['k'] = 'ok'
    r = post_chat(client,1,2,question='Frage')
    assert r.status_code == 200 and set(r.json()) == {'question','answer_html'} and r.json()['question'] == 'Frage'
    assert 'target="_blank"' in r.json()['answer_html'] and '<p>' in r.json()['answer_html']
    current['k'] = 'key'
    r = post_chat(client,1,2,question='Frage')
    assert r.status_code == 404 and r.json() == {'error':'Abschnitt nicht gefunden.'}
    current['k'] = 'temp'
    r = post_chat(client,1,2,question='Frage')
    assert r.status_code == 503 and r.headers['content-type'].startswith('application/json') and r.json() == {'error':'Meldung aus Temporary'}
    current['k'] = 'boom'
    r = quiet.post('/runs/1/sections/2/chat',data={'csrf':csrf(quiet),'question':'Frage'},headers=ORIGIN)
    assert r.status_code == 500 and 'answer_html' not in r.text


@pytest.mark.parametrize('data,valid,sent',[
    ({},False,None), ({'question':''},False,None), ({'question':'  \r\n \n '},False,None),
    ({'question':'x' * 4001},False,None), ({'question':'x' * 4000},True,'x' * 4000), ({'question':'ä' * 4000},True,'ä' * 4000),
    ({'question':'ä' * 4001},False,None), ({'question':'  a\r\n\r\nb  '},True,'a\n\nb'), ({'question':' ' + 'y' * 4000 + '\n'},True,'y' * 4000)])
def test_chat_question_validation(client,data,valid,sent):  # CHAT-017
    authorize(client)
    seen = []
    client.app.state.service.chat = lambda run_id, section_id, question: seen.append((run_id,section_id,question)) or 'Antwort'
    r = post_chat(client,3,4,**data)
    if valid:
        assert r.status_code == 200 and r.json()['question'] == sent and seen == [(3,4,sent)]
    else:
        assert r.status_code == 400 and r.json() == {'error':'Bitte eine Frage mit höchstens 4000 Zeichen eingeben.'} and seen == []


def test_chat_route_rejected_before_service_and_side_effects(client):  # CHAT-019
    seen = []
    client.app.state.service.chat = lambda *a: seen.append(a) or 'Antwort'
    url = '/runs/1/sections/1/chat'
    assert client.post(url,data={'csrf':'x','question':'F'},headers=ORIGIN).status_code == 401
    authorize(client)
    token = csrf(client)
    assert client.post(url,data={'csrf':token,'question':'F'}).status_code == 403
    assert client.post(url,data={'csrf':token,'question':'F'},headers={'Origin':'http://evil.example'}).status_code == 403
    assert client.post(url,data={'csrf':'bad','question':'F'},headers=ORIGIN).status_code == 403
    assert client.post(url,data={'question':'F'},headers=ORIGIN).status_code == 403
    assert seen == [] and client.app.state.store.query('SELECT * FROM chat_messages') == []
    assert client.post(url,data={'csrf':token,'question':'F'},headers=ORIGIN).status_code == 200 and seen == [(1,1,'F')]


def test_chat_runs_in_parallel_to_a_busy_briefing(client,monkeypatch):  # CHAT-016
    authorize(client)
    s = client.app.state.store
    rid,sid = video_section(s,transcript='Transkript')
    s.save_settings({'model':'test/model'})
    token = csrf(client)
    s.claim('report',rid)
    lock_before = s.query('SELECT * FROM work_lock')
    release_executor, reached, release_llm = threading.Event(), threading.Event(), threading.Event()
    client.app.state.executor.submit(release_executor.wait,20)
    opened, state = [], {'open':0}
    original = s.connect
    @contextmanager
    def counting():
        state['open'] += 1
        try:
            with original() as db: yield db
        finally: state['open'] -= 1
    s.connect = counting
    class LLM:
        def __init__(self,c): self.audit = []
        def close(self): pass
        def chat(self,material,turns,question):
            opened.append(state['open'])
            reached.set()
            assert release_llm.wait(20)
            return 'Fertig'
    client.app.state.service.llm_factory = LLM
    monkeypatch.setenv('OPENROUTER_API_KEY','k')
    result = []
    thread = threading.Thread(target=lambda: result.append(client.post(f'/runs/{rid}/sections/{sid}/chat',data={'csrf':token,'question':'Frage'},headers=ORIGIN)))
    try:
        thread.start()
        assert reached.wait(20)
        assert opened == [0]
        assert client.get('/health').status_code == 200
        assert client.get(f'/api/runs/{rid}/status').status_code == 200
        assert s.query('SELECT * FROM work_lock') == lock_before
    finally:
        release_llm.set()
        thread.join(20)
        release_executor.set()
        s.connect = original
    assert result[0].status_code == 200 and result[0].json()['question'] == 'Frage'
    assert [m['role'] for m in s.query('SELECT role FROM chat_messages ORDER BY id')] == ['user','assistant']
    assert s.query('SELECT * FROM work_lock') == lock_before


@pytest.mark.parametrize('text,expected',[
    ('[Repo](https://github.com/o/r)',['https://github.com/o/r']),
    ('<https://example.org/auto>',['https://example.org/auto']),
    ('Siehe https://example.org/x.',['https://example.org/x']),
    ('Siehe (https://github.com/o/r) hier',['https://github.com/o/r']),
    ('- https://example.org/liste\n- [B](https://github.com/b/c)',['https://example.org/liste','https://github.com/b/c']),
    ('| a |\n|---|\n| https://example.org/t |',['https://example.org/t']),
    ('https://foo.dev/x',[]), ('[x](https://foo.dev/x)',['https://foo.dev/x'])])
def test_render_chat_makes_https_links_clickable(text,expected):  # CHAT-020
    from app.main import render_chat
    out = render_chat(text)
    found = anchors(out)
    assert [h for h,_ in found] == expected
    for _,tag in found:
        assert 'target="_blank"' in tag and 'rel="noopener noreferrer"' in tag
    if 'Siehe https://example.org/x.' in text: assert '</a>.' in out
    if '|---|' in text: assert '<table>' in out and '<td>' in out
    if text.startswith('- '): assert '<ul>' in out and out.count('<li>') == 2


@pytest.mark.parametrize('text',[
    '[x](http://example.org)','http://example.org','[m](mailto:a@example.org)','mail a@example.org','[j](javascript:alert(1))',
    '[r](/runs/1/confirm)','[p](//example.org/x)','[u](HTTPS://example.org)','www.example.com','README.md','datei.py',
    '`https://example.org/code`','```\nhttps://example.org/block\n```','![b](https://example.org/i.png)',
    '<script>alert(1)</script>','<a href="https://example.org" onclick="x()">k</a> <img src=x onerror=alert(1)>','abcdefghijk'])
def test_render_chat_negative_controls(text):  # CHAT-021
    from app.main import render_chat
    out = render_chat(text)
    assert not re.search(r'<img|<script|<[^>]*\bon\w+=',out)
    if text.startswith('<'): assert '&lt;' in out
    if 'javascript' in text: assert '[j](javascript:alert(1))' in out
    if text.startswith('<a '):
        tags = re.findall(r'<a\b[^>]*>',out)
        assert '&lt;a href=' in out and tags == ['<a href="https://example.org" target="_blank" rel="noopener noreferrer">']
        assert not re.search(r'<[^>]*\bonclick=',out) and not re.search(r'href="[^"]*(&quot;|\')"',out)
    else: assert '<a' not in out
    if text.startswith(('[x](http:','[m]','[r]','[p]','[u]')): assert out == f'<p>{text[1]}</p>\n'
    if text.startswith(('`','```')): assert 'https://example.org' in out and '<code>' in out


def test_render_chat_keeps_code_single_escaped_and_unlinked():  # CHAT-040
    from app.main import render_chat, render_markdown
    text = ('Text [L](https://github.com/o/r) und https://example.org/p\n\n```\nprint("A") if 1 < 2 & 3 \'x\' > 0 https://example.org/block\n```\n\n'
            'Inline `a < b & "c" https://example.org/inline` Ende \ue000 \ue0000\ue000 \ue0001\ue000')
    marked, base = render_chat(text), render_markdown(text.replace('\ue000',''))
    pieces = lambda html: re.findall(r'<pre>.*?</pre>|<code>.*?</code>',html,re.DOTALL)
    assert pieces(marked) == pieces(base) and len(pieces(base)) == 2
    for piece in pieces(marked):
        assert '<a' not in piece and not re.search(r'&amp;(quot|lt|gt|amp)',piece)
    assert '&quot;A&quot;' in marked and '1 &lt; 2 &amp; 3' in marked
    assert [h for h,_ in anchors(marked)] == ['https://github.com/o/r','https://example.org/p']
    assert all('target="_blank"' in t and 'rel="noopener noreferrer"' in t for _,t in anchors(marked))
    assert '\ue000' not in marked


def test_render_chat_placeholder_does_not_join_urls():  # CHAT-042
    from app.main import render_chat, render_markdown
    for text in ('Siehe https://example.org/`code` hier','https://example.org/a?q=`y` Ende','Siehe https://example.org`code` hier','Text `a` und {`b`} Ende'):
        out = render_chat(text)
        assert not any('<' in h for h,_ in anchors(out)) and not re.search(r'<a\b[^>]*>[^<]*<code>',out)
        assert not re.search(r'<a\b[^>]*>(?:(?!</a>).)*<code>',out,re.DOTALL)
        assert re.findall(r'<code>.*?</code>',out,re.DOTALL) == re.findall(r'<code>.*?</code>',render_markdown(text),re.DOTALL)
        assert '\ue000' not in out
    assert '{<code>b</code>}' in render_chat('Text `a` und {`b`} Ende')


def test_render_chat_leaves_quoted_bare_urls_as_text():  # CHAT-041
    from app.main import render_chat
    out = render_chat('Siehe "https://example.org/q" und \'https://example.org/s\' sowie (https://example.org/k) und [x](https://example.org/q)')
    assert [h for h,_ in anchors(out)] == ['https://example.org/k','https://example.org/q']
    assert '&quot;https://example.org/q&quot;' in out and "'https://example.org/s'" in out
    assert not re.search(r'href="[^"]*(&quot;|\'|")"',out)


def test_show_run_renders_only_own_history_escaped(client):  # CHAT-022
    authorize(client)
    s = client.app.state.store
    rid,one = video_section(s,transcript='T')
    _,two = video_section(s,markdown='Video-ID: lmnopqrstuv\n\nZwei',video='lmnopqrstuv',title='Zwei',position=1,rid=rid)
    _,plain = video_section(s,markdown='Kein Video',video=None,title='Ohne',position=2,rid=rid)
    other_rid,other = video_section(s,transcript='T')
    message(s,one,'user','<b>fett</b>\nzweite Zeile')
    message(s,one,'assistant','Antwort [L](https://example.org/x)')
    message(s,two,'user','Frage zwei')
    message(s,other,'user','FREMD')
    message(s,one,'user','Dritte')
    message(s,one,'assistant','Letzte')
    text = client.get(f'/runs/{rid}').text
    assert 'FREMD' not in text and text.count('class="chat"') == 2
    first, second = [c.split('</details>')[0] for c in text.split('<details class="chat">')[1:]]
    assert '<div class="chat-user">&lt;b&gt;fett&lt;/b&gt;\nzweite Zeile</div>' in first
    assert first.index('fett') < first.index('Antwort') < first.index('Dritte') < first.index('Letzte')
    from app.main import render_chat
    assert f'<div class="chat-assistant">{render_chat("Antwort [L](https://example.org/x)")}</div>' in first
    assert '(2 Fragen)' in first and '(1 Frage)' in second and 'chat-empty' not in first and 'chat-note' not in first
    assert 'data-pending="Antwort wird erstellt …"' in first
    assert 'chat-note' in second and 'data-pending="Transkript wird einmalig nachgeladen, danach wird die Antwort erstellt …"' in second
    empty = client.get(f'/runs/{other_rid}').text
    assert 'FREMD' in empty and 'Frag etwas zu diesem Video.' not in empty
    s.execute('DELETE FROM chat_messages WHERE section_id=?',(other,))
    empty = client.get(f'/runs/{other_rid}').text
    assert 'Frag etwas zu diesem Video.' in empty and 'Die Antworten stützen sich auf Transkript und Zusammenfassung dieses Videos.' in empty
    assert 'Mit diesem Video chatten</summary>' in empty
    assert 'Ohne' in text and f'/sections/{plain}/chat' not in text


def test_chat_links_clickable_only_in_chat_answers(client):  # CHAT-033
    from app.main import render_markdown
    assert 'target=' not in render_markdown('[a](https://a.com)')
    authorize(client)
    s = client.app.state.store
    md = 'Video-ID: abcdefghijk\n\n[a](https://a.com) und https://example.org/x\n\n### Links aus dem Video\n\n- [Repo](<https://github.com/o/r>)'
    rid,sid = video_section(s,markdown=md)
    message(s,sid,'assistant','[a](https://a.com) und https://example.org/x')
    text = client.get(f'/runs/{rid}').text
    summary, chat = text.split('<details class="chat">')
    summary = summary.split('<article class="prose">',1)[1]
    assert [h for h,_ in anchors(summary) if 'youtube.com' not in h] == ['https://a.com','https://github.com/o/r']
    assert all('target=' not in tag and 'rel=' not in tag for h,tag in anchors(summary) if 'youtube.com' not in h)
    assert 'https://example.org/x' in summary
    chat_links = anchors(chat.split('<div class="chat-assistant">',1)[1].split('</div>')[0])
    assert [h for h,_ in chat_links] == ['https://a.com','https://example.org/x']
    assert all('target="_blank"' in tag and 'rel="noopener noreferrer"' in tag for _,tag in chat_links)


def test_chat_front_end_files_static_contract():  # CHAT-034
    root = Path(__file__).parent.parent / 'app'
    js = (root / 'static/app.js').read_text().splitlines()
    for needle in ('form:not(.chat-form)','.chat[open]','isComposing','requestSubmit'):
        assert sum(needle in line for line in js) == 1, needle
    assert (root / 'templates/report.html').read_text().count('<textarea') == 1
    assert (root / 'templates/icons.html').read_text().count("name == 'send'") == 1
    report = (root / 'templates/report.html').read_text()
    assert 'style=' not in report and '<style' not in report and '<script' not in report
    if shutil.which('node'):
        assert subprocess.run(['node','--check',str(root / 'static/app.js')],check=False).returncode == 0


def test_chat_markup_contract(client):  # CHAT-035
    authorize(client)
    s = client.app.state.store
    rid,one = video_section(s,transcript='T')
    _,two = video_section(s,markdown='Video-ID: lmnopqrstuv\n\nZwei',video='lmnopqrstuv',position=1,rid=rid)
    s.execute('UPDATE sections SET read_at=? WHERE id=?',('2026',two))
    token = csrf(client)
    text = client.get(f'/runs/{rid}').text
    blocks = [c.split('</details>')[0] for c in text.split('<details class="chat">')[1:]]
    assert len(blocks) == 2
    hints = []
    for block,sid in zip(blocks,(one,two)):
        assert '<div class="chat-log" role="log">' in block
        assert f'<form class="chat-form" method="post" action="/runs/{rid}/sections/{sid}/chat"' in block
        assert f'<input type="hidden" name="csrf" value="{token}">' in block
        area = re.search(r'<textarea[^>]*>',block).group(0)
        for attr in ('name="question"','rows="1"','maxlength="4000"',' required','aria-label="Deine Frage zu diesem Video"'):
            assert attr in area
        described = re.search(r'aria-describedby="([^"]+)"',area).group(1)
        assert f'<p class="chat-hint" id="{described}">' in block
        hints.append(described)
        button = re.search(r'<button aria-label="Frage senden">(.*?)</button>',block,re.DOTALL).group(1)
        assert button.startswith('<svg')
        assert '<label' not in block and 'style=' not in block
    assert hints[0] != hints[1]


def test_history_and_store_run_do_not_carry_chat(client):  # CHAT-039
    authorize(client)
    s = client.app.state.store
    rid,sid = video_section(s,transcript='T')
    message(s,sid,'user','Frage')
    r = client.get('/history')
    assert r.status_code == 200 and 'class="chat"' not in r.text
    assert all('chat' not in section and 'transcript' not in section for section in s.run(rid)['sections'])


def test_docs_describe_chat_without_new_model_setting():  # CHAT-024
    root = Path(__file__).parent.parent
    readme, transcripts = (root / 'README.md').read_text(), (root / 'docs/TRANSCRIPTS.md').read_text()
    for word in ('Chat','Setup gespeicherte Modell','nachgeladen','nicht geprüft','Chatverläufe','Datenbank'):
        assert word in readme
    for word in ('Klartext','Löschfunktion','OpenRouter','0,02 USD','beim ersten Chat'):
        assert word in transcripts
    assert 'CHAT_MODEL' not in readme and readme.count('OPENROUTER_DECISIONS_MODEL') >= 2
