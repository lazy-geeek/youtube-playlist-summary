import base64
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode, urlsplit, parse_qs
import bleach
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from .db import Store, Busy, now
from .providers import YouTube, Temporary
from .service import Service, probe_config

load_dotenv()
ROOT = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(ROOT / 'templates'))
APP_URL = os.environ.get('APP_URL', 'http://localhost:8765').rstrip('/')
SECURE = APP_URL.startswith('https://')
DIRECTORY = Path(os.environ.get('DATA_DIR', './data'))


@asynccontextmanager
async def lifespan(app):
    password = os.environ.get('APP_PASSWORD', '')
    key = os.environ.get('ENCRYPTION_KEY', '')
    if len(password) < 16 or not key:
        raise RuntimeError('APP_PASSWORD (mindestens 16 Zeichen) und ENCRYPTION_KEY erforderlich.')
    app.state.store = Store(DIRECTORY, key)
    # Prevent a second process or rolling deployment from recovering a live job.
    lock = (DIRECTORY / 'worker.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError('Die Datenbank wird bereits von einer App-Instanz verwendet.')
    app.state.store.recover()
    app.state.service = Service(app.state.store, DIRECTORY)
    app.state.executor = ThreadPoolExecutor(max_workers=1)
    yield
    app.state.executor.shutdown(wait=True)
    fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')


def auth_challenge():
    return PlainTextResponse('Benutzername und Passwort erforderlich.', status_code=401,
            headers={'WWW-Authenticate': 'Basic realm="Playlist Briefing", charset="UTF-8"', 'Cache-Control': 'no-store'})


@app.middleware('http')
async def guard(request, call_next):
    header = request.headers.get('authorization', '')
    try:
        scheme, raw = header.split(' ',1)
        user, password = base64.b64decode(raw,validate=True).decode().split(':',1)
        ok = scheme.lower() == 'basic' and hmac.compare_digest(user.encode(),os.environ.get('APP_USERNAME','owner').encode()) and hmac.compare_digest(password.encode(),os.environ.get('APP_PASSWORD','').encode())
    except (ValueError, UnicodeError):
        ok = False
    store = request.app.state.store
    token = request.cookies.get('briefing_session', '')
    sid = hashlib.sha256((token + os.environ.get('APP_USERNAME','owner') + os.environ.get('APP_PASSWORD','')).encode()).hexdigest()
    rows = store.query('SELECT * FROM sessions WHERE id=? AND expires>?', (sid,time.time()))
    fresh = not rows
    if fresh and not ok:
        return auth_challenge()
    if fresh:
        token = secrets.token_urlsafe(32)
        sid = hashlib.sha256((token + os.environ.get('APP_USERNAME','owner') + os.environ.get('APP_PASSWORD','')).encode()).hexdigest()
        csrf = secrets.token_urlsafe(32)
        store.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
        store.execute('INSERT INTO sessions(id,csrf,expires) VALUES (?,?,?)', (sid,csrf,time.time()+30*86400))
        session = {'id':sid,'csrf':csrf,'oauth_state':None}
    else:
        session = rows[0]
    request.state.session = session
    if request.method == 'POST':
        origin = request.headers.get('origin')
        if origin != APP_URL:
            return PlainTextResponse('Ungültiger Anfrageursprung.',status_code=403)
        await request.body()
        form = await request.form()
        if not hmac.compare_digest(str(form.get('csrf','')),session['csrf']):
            return PlainTextResponse('Ungültiger Formularschutz.',status_code=403)
    response = await call_next(request)
    if fresh:
        response.set_cookie('briefing_session',token,httponly=True,secure=SECURE,samesite='lax',max_age=30*86400)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data: https://i.ytimg.com; base-uri 'none'; frame-ancestors 'none'; form-action 'self' https://accounts.google.com"
    if SECURE:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    return response


def page(request, template, **context):
    return TEMPLATES.TemplateResponse(request=request,name=template,context={
        'csrf':request.state.session['csrf'], 'busy':bool(request.app.state.store.query('SELECT * FROM work_lock')),
        'labels':{'success':'Ausgewertet','unavailable':'Nicht auswertbar','temporary':'Vorübergehend fehlgeschlagen',
                  'pending':'Ausstehend','running':'In Bearbeitung','ready':'Bericht verfügbar','failed':'Fehlgeschlagen',
                  'interrupted':'Unterbrochen','partial':'Teilweise archiviert','done':'Archiviert','added':'Im Ziel angelegt',
                  'inserting':'Hinzufügen wird abgeglichen'}, **context})


@app.exception_handler(Temporary)
@app.exception_handler(Busy)
async def expected_error(request, exc):
    return page(request,'error.html',message=str(exc))


@app.get('/health')
def health():
    return {'status':'ok'}


@app.get('/',response_class=HTMLResponse)
def home(request:Request):
    s = request.app.state.store
    c = s.settings()
    return page(request,'home.html',configured=bool(os.environ.get('OPENROUTER_API_KEY')) and all(c.get(k) for k in ['source','archive','unavailable','model','refresh_token']),
        max_videos=c.get('max_videos',''),google=bool(c.get('refresh_token')),tested=s.probe_ready(probe_config(c)),
        runs=s.query('SELECT * FROM runs ORDER BY id DESC LIMIT 6'))


@app.get('/api/count')
def count(request:Request):
    s = request.app.state.store
    c = s.settings()
    if not c.get('source') or not c.get('refresh_token'):
        return JSONResponse({'error':'Setup und Google-Anmeldung fehlen.'},status_code=409)
    yt = YouTube(s)
    try:
        entries = yt.entries(c['source'])
        return {'total':len(entries),'pending':len(entries),'held':0}
    except Temporary as exc:
        return JSONResponse({'error':str(exc)},status_code=503)
    finally:
        yt.close()


@app.get('/setup',response_class=HTMLResponse)
def setup(request:Request):
    s = request.app.state.store
    config = s.settings()
    safe = {k:config.get(k,'') for k in ['source','archive','unavailable','model','search_engine','transcript_mode','google_client_id','google_allowed_email','max_videos']}
    safe['google_allowed_email'] = safe['google_allowed_email'] or os.environ.get('GOOGLE_ALLOWED_EMAIL','')
    probes = s.query('SELECT * FROM probes ORDER BY id DESC LIMIT 1')
    return page(request,'setup.html',config=safe,google=bool(config.get('refresh_token')),
        apify_set=bool(os.environ.get('APIFY_API_TOKEN')),key_set=bool(os.environ.get('OPENROUTER_API_KEY')),secret_set=bool(config.get('google_client_secret')),
        results=json.loads(probes[0]['results']) if probes else [],redirect_uri=APP_URL+'/oauth/callback',
        playlist_locked=bool(s.query('SELECT id FROM runs LIMIT 1')))


@app.post('/setup')
async def save_setup(request:Request):
    form = await request.form()
    s = request.app.state.store
    if s.query('SELECT * FROM work_lock'):
        raise Busy('Setup während eines Durchlaufs gesperrt.')
    old = s.settings()
    values = {k:str(form.get(k,'')).strip() for k in ['source','archive','unavailable','model','search_engine','transcript_mode','google_client_id','google_allowed_email','max_videos']}
    if values['max_videos']:
        try:
            limit = int(values['max_videos'])
        except ValueError:
            raise Temporary('Maximale Videoanzahl muss eine ganze Zahl sein.')
        if not 1 <= limit <= 10000:
            raise Temporary('Maximale Videoanzahl muss zwischen 1 und 10000 liegen; leer bedeutet alle.')
        values['max_videos'] = str(limit)
    for key in ('source', 'archive', 'unavailable'):
        parsed = urlsplit(values[key])
        if parsed.hostname in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be'):
            playlist = parse_qs(parsed.query).get('list', [''])[0]
            if playlist:
                values[key] = playlist
    if s.query('SELECT id FROM runs LIMIT 1') and any(values[k] != old.get(k) for k in ['source','archive','unavailable']):
        raise Temporary('Die festen Playlists können nach dem ersten Durchlauf nicht geändert werden.')
    if values['transcript_mode'] not in ('import','public','apify') or values['search_engine'] not in ('searxng','auto','exa','firecrawl','parallel','perplexity'):
        raise Temporary('Ungültige Transkript- oder Suchkonfiguration.')
    if values['google_allowed_email'] and '@' not in values['google_allowed_email']:
        raise Temporary('Eine gültige berechtigte Google-Adresse angeben.')
    for k in ['google_client_secret']:
        if form.get(k):
            values[k] = str(form[k]).strip()
    if old.get('google_client_id') != values['google_client_id'] or old.get('google_allowed_email') != values['google_allowed_email']:
        values.update({'refresh_token':'','access_token':'','token_expires':'0','google_subject':''})
    s.save_settings(values)
    return RedirectResponse('/setup',status_code=303)


@app.post('/oauth/start')
def oauth_start(request:Request):
    c = request.app.state.store.settings()
    if not all(c.get(k) for k in ('google_client_id','google_client_secret','google_allowed_email')):
        raise Temporary('Google-Zugang und berechtigte E-Mail zuerst im Setup speichern.')
    state = secrets.token_urlsafe(32)
    request.app.state.store.execute('UPDATE sessions SET oauth_state=? WHERE id=?',
                                   (json.dumps({'state':state,'expires':time.time()+600}),request.state.session['id']))
    params = {'client_id':c['google_client_id'],'redirect_uri':APP_URL+'/oauth/callback','response_type':'code',
              'scope':'openid email https://www.googleapis.com/auth/youtube.force-ssl',
              'access_type':'offline','prompt':'consent','state':state,'login_hint':c['google_allowed_email']}
    return RedirectResponse('https://accounts.google.com/o/oauth2/v2/auth?'+urlencode(params),status_code=303)


@app.get('/oauth/callback')
def callback(request:Request, state:str='', code:str='', error:str=''):
    s = request.app.state.store
    stored = request.state.session.get('oauth_state')
    s.execute('UPDATE sessions SET oauth_state=NULL WHERE id=?',(request.state.session['id'],))
    try:
        expected = json.loads(stored or '{}')
        ok = expected.get('expires',0) > time.time() and hmac.compare_digest(state,expected.get('state',''))
    except ValueError:
        ok = False
    if not ok or not code or error:
        raise Temporary('Google-Anmeldung abgebrochen oder ungültig. Bitte neu starten.')
    c = s.settings()
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post('https://oauth2.googleapis.com/token',data={'code':code,'client_id':c['google_client_id'],
                'client_secret':c['google_client_secret'],'redirect_uri':APP_URL+'/oauth/callback','grant_type':'authorization_code'})
            if r.status_code != 200:
                raise Temporary('Google-Anmeldung fehlgeschlagen.')
            token = r.json()
            u = client.get('https://openidconnect.googleapis.com/v1/userinfo',headers={'Authorization':'Bearer '+token['access_token']})
            if u.status_code != 200:
                raise Temporary('Google-Identität konnte nicht geprüft werden.')
            identity = u.json()
        if identity.get('email_verified') is not True or identity.get('email','').casefold() != c['google_allowed_email'].casefold():
            raise Temporary('Dieses Google-Konto ist nicht für die App berechtigt.')
        if c.get('google_subject') and c['google_subject'] != identity['sub']:
            raise Temporary('Google-Konto darf nach Einrichtung nicht gewechselt werden.')
        refresh = token.get('refresh_token') or c.get('refresh_token')
        if not refresh:
            raise Temporary('Google hat keinen Offline-Zugang erteilt; Zugriff widerrufen und erneut verbinden.')
        s.save_settings({'access_token':token['access_token'],'refresh_token':refresh,
                        'token_expires':str(time.time()+token['expires_in']),'google_subject':identity['sub']})
    except httpx.HTTPError:
        raise Temporary('Google-Verbindung derzeit nicht möglich.')
    return RedirectResponse('/setup',status_code=303)


@app.post('/probe')
def probe(request:Request):
    request.app.state.service.check()
    return RedirectResponse('/setup',status_code=303)


@app.post('/runs')
def create_run(request:Request):
    run_id, config = request.app.state.service.create()
    request.app.state.executor.submit(request.app.state.service.generate,run_id,config)
    return RedirectResponse('/runs/'+str(run_id),status_code=303)


@app.get('/history',response_class=HTMLResponse)
def history(request:Request):
    s = request.app.state.store
    runs = [s.run(r['id']) for r in s.query('SELECT id FROM runs ORDER BY id DESC')]
    return page(request,'history.html',runs=runs)


def link_video_ids(html, items):
    import re
    ids = {i['video_id'] for i in items if re.fullmatch(r'[A-Za-z0-9_-]{11}', i['video_id'])}
    if not ids:
        return html
    pattern = re.compile(r'(?<![A-Za-z0-9_-])(' + '|'.join(re.escape(v) for v in sorted(ids)) + r')(?![A-Za-z0-9_-])')
    # Work on text nodes only; never replace existing link text or attributes.
    parts = re.split(r'(<[^>]+>)', html)
    in_link = False
    for idx, part in enumerate(parts):
        if part.startswith('<'):
            if re.match(r'<a(?:\s|>)', part): in_link = True
            elif part.startswith('</a'): in_link = False
        elif not in_link:
            parts[idx] = pattern.sub(lambda m: '<a href="https://www.youtube.com/watch?v=' + m[0]
                + '" target="_blank" rel="noopener noreferrer">' + m[0] + '</a>', part)
    return ''.join(parts)


@app.get('/runs/{run_id}',response_class=HTMLResponse)
def show_run(request:Request,run_id:int):
    try:
        r = request.app.state.store.run(run_id)
    except KeyError:
        raise HTTPException(404,'Bericht nicht gefunden.')
    rendered = MarkdownIt('commonmark',{'html':False}).render(r['markdown'])
    rendered = bleach.clean(rendered,tags=['h1','h2','h3','p','ul','ol','li','strong','em','code','pre','blockquote','a','hr','br'],
                            attributes={'a':['href','title']},protocols=['https'],strip=True)
    for section in r['sections']:
        import re
        video = next((i for i in r['items'] if section['markdown'].startswith('Video-ID: ' + i['video_id'] + '\n') and re.fullmatch(r'[A-Za-z0-9_-]{11}', i['video_id'])), None)
        section['video'] = video
        if video:
            section['title'] = video['title']
            section['thumbnail'] = 'https://i.ytimg.com/vi/' + video['video_id'] + '/hqdefault.jpg'
        section['html'] = bleach.clean(MarkdownIt('commonmark',{'html':False}).render(section['markdown']),
            tags=['h1','h2','h3','p','ul','ol','li','strong','em','code','pre','blockquote','a','hr','br'],
            attributes={'a':['href','title']},protocols=['https'],strip=True)
        section['html'] = link_video_ids(section['html'], r['items'])
    rendered = link_video_ids(rendered, r['items'])
    return page(request,'report.html',run=r,rendered=rendered)


@app.get('/api/runs/{run_id}/status')
def run_status(request:Request,run_id:int):
    try:
        run = request.app.state.store.run(run_id)
    except KeyError:
        raise HTTPException(404,'Bericht nicht gefunden.')
    return {'status':run['status'], 'archive_status':run['archive_status'],
            'counts':run['counts'], 'error':run['error']}


@app.get('/runs/{run_id}/markdown')
def export(request:Request,run_id:int):
    try:
        r = request.app.state.store.run(run_id)
    except KeyError:
        raise HTTPException(404,'Bericht nicht gefunden.')
    return PlainTextResponse(r['markdown'],media_type='text/markdown; charset=utf-8',
                             headers={'Content-Disposition':f'attachment; filename="playlist-bericht-{run_id}.md"'})


@app.post('/runs/{run_id}/confirm')
def confirm(request:Request,run_id:int):
    try:
        request.app.state.service.confirm(run_id)
    except KeyError:
        raise HTTPException(404,'Bericht nicht gefunden.')
    request.app.state.executor.submit(request.app.state.service.archive,run_id)
    return RedirectResponse('/runs/'+str(run_id),status_code=303)


@app.post('/runs/{run_id}/sections/{section_id}/read')
async def read_section(request:Request,run_id:int,section_id:int):
    s = request.app.state.store
    form = await request.form()
    rows = s.query('SELECT id FROM sections WHERE id=? AND run_id=?',(section_id,run_id))
    if not rows:
        raise HTTPException(404,'Abschnitt nicht gefunden.')
    with s.connect() as db:
        db.execute('UPDATE sections SET read_at=? WHERE id=? AND run_id=?',
                   (None if form.get('unread') else now(),section_id,run_id))
        unread = db.execute('SELECT COUNT(*) FROM sections WHERE run_id=? AND read_at IS NULL',(run_id,)).fetchone()[0]
        if unread:
            db.execute('UPDATE runs SET read_completed=NULL WHERE id=?',(run_id,))
        else:
            db.execute('UPDATE runs SET read_completed=COALESCE(read_completed,?) WHERE id=?',(now(),run_id))
    return RedirectResponse('/runs/'+str(run_id),status_code=303)
