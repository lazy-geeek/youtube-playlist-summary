import json
import os
import re
from pathlib import Path
from .db import Busy, now
from .providers import YouTube, Transcripts, OpenRouter, Links, Temporary, Unavailable, source_urls


def probe_config(config):
    result = {k: config.get(k, '') for k in ('source', 'transcript_mode', 'google_subject')}
    if config.get('transcript_mode') == 'apify':
        result['apify_actor'] = os.environ.get('APIFY_TRANSCRIPT_ACTOR', 'starvibe/youtube-video-transcript')
        result['apify_language'] = os.environ.get('APIFY_TRANSCRIPT_LANGUAGE', 'en').strip() or 'en'
    return result


class Service:
    def __init__(self, store, directory, youtube_factory=YouTube, llm_factory=OpenRouter):
        self.store = store
        self.directory = Path(directory)
        self.youtube_factory = youtube_factory
        self.llm_factory = llm_factory

    def check(self):
        if not all(self.store.settings().get(k) for k in ('source', 'archive', 'unavailable', 'refresh_token')):
            raise Temporary('Zuerst alle drei Playlists speichern und Google verbinden.')
        self.store.claim('probe')
        yt = self.youtube_factory(self.store)
        try:
            c = self.store.settings()
            yt.validate_playlists([c[k] for k in ('source', 'archive', 'unavailable')])
            entries = yt.entries(c['source'])[:3]
            if not entries:
                raise Temporary('Quell-Playlist ist leer; kein Transkripttest möglich.')
            transcripts = Transcripts(c.get('transcript_mode', ''), self.directory)
            results = []
            for e in entries:
                try:
                    text, origin = transcripts.fetch(e['video_id'])
                    results.append({**e, 'status': 'success', 'words': len(text.split()), 'origin': origin})
                except Unavailable as exc:
                    results.append({**e, 'status': 'unavailable', 'reason': str(exc)})
                except Temporary as exc:
                    results.append({**e, 'status': 'temporary', 'reason': str(exc)})
            self.store.execute('INSERT INTO probes(created,config,results,success) VALUES (?,?,?,?)',
                (now(), json.dumps(probe_config(c)), json.dumps(results, ensure_ascii=False), int(any(r['status'] == 'success' for r in results))))
            return results
        finally:
            yt.close()
            self.store.release()

    def create(self):
        c = self.store.settings()
        c['openrouter_key'] = os.environ.get('OPENROUTER_API_KEY','')
        if not all(c.get(k) for k in ('source','archive','unavailable','model','openrouter_key','refresh_token')):
            raise Temporary('Setup und Google-Verbindung zuerst vervollständigen.')
        if not self.store.probe_ready(probe_config(c)):
            raise Temporary('Zuerst einen erfolgreichen Transkripttest mit echten Playlist-Videos durchführen.')
        self.store.claim('report')
        # Snapshot excludes all credentials; destination IDs remain immutable per run.
        snapshot = {k: c.get(k, '') for k in ('source','archive','unavailable','model','search_engine','transcript_mode','google_subject','max_videos')}
        try:
            run_id = self.store.execute('INSERT INTO runs(created,status,config) VALUES (?,?,?)', (now(),'running',json.dumps(snapshot)))
            self.store.execute('UPDATE work_lock SET run_id=? WHERE id=1', (run_id,))
            return run_id, c
        except Exception:
            self.store.release()
            raise

    def generate(self, run_id, c):
        yt, llm = self.youtube_factory(self.store), self.llm_factory(c)
        try:
            yt.validate_playlists([c[k] for k in ('source','archive','unavailable')])
            entries = yt.entries(c['source'])
            if c.get('max_videos'):
                entries = entries[:int(c['max_videos'])]
            if not entries:
                raise Temporary('Quell-Playlist ist leer.')
            for e in entries:
                self.store.execute('INSERT INTO items(run_id,source_item,video_id,title) VALUES (?,?,?,?)',
                                   (run_id,e['source_item'],e['video_id'],e['title']))
            transcripts = Transcripts(c['transcript_mode'], self.directory)
            all_findings, all_links = [], []
            for item in self.store.run(run_id)['items']:
                status, reason, evidence = 'temporary', '', {}
                try:
                    text, origin = transcripts.fetch(item['video_id'])
                    meta = yt.metadata(item['video_id'])
                    comments, note = yt.comments(item['video_id'], meta['channel_id'])
                    meta['creator_comments'] = comments
                    allowed = source_urls(text + '\n' + meta['description'] + '\n' + '\n'.join(comments))
                    findings, resources = llm.extract(item['video_id'], text, meta)
                    # Also check exact links found in description, creator comments or transcript,
                    # even if extractor failed to nominate them.
                    named_urls = {r.get('url') for r in resources}
                    for url in sorted(allowed - named_urls):
                        resources.append({'name': urlsplit_name(url), 'url': url,
                                          'context': meta['title'] + '\n' + meta['description'][:3000]})
                    links = [Links(llm).verify(r, allowed) for r in resources]
                    for f in findings:
                        f['id'] = f"v{item['id']}-f{len(all_findings)}"
                        all_findings.append(f)
                    for link in links:
                        link['video_id'] = item['video_id']
                    all_links.extend(links)
                    evidence = {'origin': origin, 'metadata': meta, 'comment_note': note,
                                'findings': findings, 'resources': links}
                    status = 'success'
                except Unavailable as exc:
                    status, reason = 'unavailable', str(exc)
                except Temporary as exc:
                    reason = str(exc)
                except Exception:
                    reason = 'Unerwarteter Verarbeitungsfehler; später erneut versuchen.'
                target = c['archive'] if status == 'success' else c['unavailable'] if status == 'unavailable' else None
                self.store.execute('UPDATE items SET status=?,reason=?,evidence=?,target=? WHERE id=?',
                                   (status,reason,json.dumps(evidence,ensure_ascii=False),target,item['id']))
            counts = self.store.run(run_id)['counts']
            if all_findings:
                markdown, provenance = self.synthesize(llm, all_findings, all_links)
            else:
                markdown = '# Playlist-Bericht\n\nEs konnte kein Video inhaltlich ausgewertet werden. Es wurde keine Zusammenfassung aus Titeln oder Beschreibungen erzeugt.\n'
                provenance = []
            markdown += (f"\n\n## Auswertung dieses Durchlaufs\n\n{counts['success']} Videos ausgewertet, "
                         f"{counts['unavailable']} sicher ohne verwertbare Untertitel, {counts['temporary']} vorübergehend fehlgeschlagen. "
                         'Fehlgeschlagene Videos bleiben für einen späteren Versuch in der Quelle.\n')
            snapshot = json.loads(self.store.run(run_id)['config'])
            snapshot.update({'provenance': provenance, 'model_calls': llm.audit, 'youtube_quota_estimate': yt.units})
            with self.store.connect() as db:
                parts = re.split(r'^## ', markdown, flags=re.MULTILINE)
                intro = re.sub(r'^# [^\n]*\n', '', parts[0]).strip()
                report_parts = ([('Gesamtüberblick',intro)] if intro else [])
                for part in parts[1:]:
                    title, _, body = part.partition('\n')
                    report_parts.append((title.strip(),body.strip()))
                for position, (title, body) in enumerate(report_parts):
                    db.execute('INSERT INTO sections(run_id,position,title,markdown) VALUES (?,?,?,?)',
                               (run_id,position,title.strip(),body.strip()))
                if len(parts) == 1:
                    db.execute('INSERT INTO sections(run_id,position,title,markdown) VALUES (?,?,?,?)',
                               (run_id,0,'Ergebnis',markdown))
                db.execute("UPDATE runs SET status='ready',markdown=?,config=? WHERE id=?",
                           (markdown,json.dumps(snapshot,ensure_ascii=False),run_id))
        except Exception as exc:
            error = str(exc) if isinstance(exc, Temporary) else 'Berichtserstellung fehlgeschlagen. Bitte erneut starten.'
            self.store.execute("UPDATE runs SET status='failed',error=? WHERE id=?", (error,run_id))
        finally:
            yt.close()
            llm.close()
            self.store.release()

        if self.store.run(run_id)['status'] == 'ready':
            self.confirm(run_id)
            self.archive(run_id)

    def synthesize(self, llm, findings, links):
        valid = {f['id']: f for f in findings}
        # Hierarchical thematic reduction includes every successfully analysed video.
        groups = []
        for start in range(0, len(findings), 60):
            batch = findings[start:start+60]
            data = llm.call('Führe diese belegten Befunde thematisch zusammen. Ausreichend Substanz, Nutzen, Grenzen, '
                'anschauliche Beispiele statt Klickanleitungen. Nur konkrete Themenblöcke, kein Gesamtüberblick. Keine Einzelvideo-Zusammenfassungen, keine URLs, '
                'keine Zeitmarken. JSON {"sections":[{"title":"...","paragraphs":[{"text":"deutscher Text",'
                '"finding_ids":["..."]}]}]}. Jeder Absatz muss belegende finding_ids aus den Daten enthalten.\n'
                + json.dumps(batch,ensure_ascii=False))
            groups.extend(self.validate_sections(data,valid))
        if len(groups) > 1:
            sourced_groups = [{**g, 'source_findings': [valid[fid] for fid in dict.fromkeys(
                fid for p in g['paragraphs'] for fid in p['finding_ids'])]} for g in groups]
            data = llm.call('Erstelle einen eigenständig lesbaren deutschen GESAMTBERICHT. Beginne direkt mit konkreten Themenblöcken. Kein Gesamtüberblick und keine allgemeine Einleitung. '
                'Führe Überschneidungen zusammen, erläutere neue KI-Entwicklungen, Tools und Arbeitsweisen mit Nutzen, '
                'Grenzen und Beispielen. Eigenständige Themen angemessen behandeln. Keine Einzelvideo-Liste, keine URLs, '
                'Zeitmarken oder Markdown-Links. Meinungen und Unsicherheit ausdrücklich kennzeichnen. '
                'Originalzitate und Video-IDs stehen in source_findings; verwende sie als Belege. '
                'Erhalte den Inhalt ALLER gelieferten Gruppen. JSON {"sections":[{"title":"...",'
                '"paragraphs":[{"text":"...","finding_ids":["..."]}]}]}.\n' + json.dumps(sourced_groups,ensure_ascii=False))
            groups = self.validate_sections(data,valid)
        groups = [g for g in groups if g['title'].strip().casefold() not in ('gesamtüberblick', 'gesamtueberblick')]
        if not groups:
            raise Temporary('Kein belegbarer Gesamtbericht erhalten.')
        represented = {valid[fid]['video_id'] for g in groups for p in g['paragraphs'] for fid in p['finding_ids']}
        if represented != {f['video_id'] for f in findings}:
            raise Temporary('Gesamtbericht deckt nicht alle erfolgreich analysierten Videos ab; bitte erneut versuchen.')
        markdown = '# Playlist-Bericht\n'
        provenance = []
        for group in groups:
            markdown += '\n## ' + clean_prose(group['title']) + '\n\n'
            for p in group['paragraphs']:
                text = clean_prose(p['text'])
                markdown += text + '\n\n'
                provenance.append({'text':text,'findings':[valid[fid] for fid in p['finding_ids']]})
        if links:
            markdown += '\n## Tools und zentrale Ressourcen\n\n'
            seen = set()
            for r in links:
                identity = r['url'] or r['name'].casefold()
                if identity in seen:
                    continue
                seen.add(identity)
                name = clean_prose(r['name']).replace('[','').replace(']','').replace('\n',' ')
                if not name.strip():
                    continue
                if r['verified']:
                    markdown += f"- [{name}](<{r['url']}>) — URL und Projektbezug geprüft.\n"
                else:
                    markdown += f'- {name} — Identität beziehungsweise URL nicht sicher verifiziert.\n'
        return markdown, provenance

    @staticmethod
    def validate_sections(data, valid):
        result = []
        for section in data.get('sections', []) if isinstance(data, dict) else []:
            if not isinstance(section, dict) or not isinstance(section.get('title'), str):
                continue
            paragraphs = []
            for p in section.get('paragraphs', []):
                if isinstance(p, dict) and isinstance(p.get('text'), str) and isinstance(p.get('finding_ids'), list) and p['finding_ids'] and all(isinstance(fid,str) and fid in valid for fid in p['finding_ids']):
                    paragraphs.append(p)
            if paragraphs:
                result.append({'title': section['title'], 'paragraphs': paragraphs})
        return result

    def confirm(self, run_id):
        self.store.claim('archive', run_id)
        try:
            r = self.store.run(run_id)
            if r['status'] != 'ready':
                raise Temporary('Nur fertige Berichte können bestätigt werden.')
            self.store.execute("UPDATE runs SET confirmed=COALESCE(confirmed,?),archive_status='running',error='' WHERE id=?", (now(),run_id))
        except Exception:
            self.store.release()
            raise

    def archive(self, run_id):
        yt = self.youtube_factory(self.store)
        try:
            r = self.store.run(run_id)
            if not r['confirmed']:
                raise Temporary('Bericht wurde noch nicht bestätigt.')
            c = json.loads(r['config'])
            if self.store.settings().get('google_subject') != c['google_subject']:
                raise Temporary('Google-Konto stimmt nicht mit dem Durchlauf überein.')
            yt.validate_playlists([c[k] for k in ('source','archive','unavailable')])
            errors = []
            for item in r['items']:
                if item['status'] not in ('success','unavailable') or item['move_status'] == 'done':
                    continue
                try:
                    # Always reconcile destination live, even if a previous response was stored.
                    target_item = yt.target_item(item['target'],item['video_id'])
                    if not target_item:
                        if item['move_status'] == 'inserting':
                            raise Temporary('Ergebnis eines früheren Hinzufügens unklar; kein erneutes Hinzufügen. Ziel später erneut prüfen.')
                        self.store.execute("UPDATE items SET move_status='inserting' WHERE id=?", (item['id'],))
                        target_item = yt.insert(item['target'],item['video_id'])
                    self.store.execute("UPDATE items SET move_status='added',target_item=?,reason='' WHERE id=?", (target_item,item['id']))
                    # Verify target exists before deleting the exact source playlist item.
                    if not yt.target_item(item['target'],item['video_id']):
                        raise Temporary('Ziel-Eintrag noch nicht bestätigt; Quelle bleibt erhalten.')
                    if yt.source_present(item['source_item'],c['source'],item['video_id']):
                        yt.delete_item(item['source_item'])
                    self.store.execute("UPDATE items SET move_status='done',reason='' WHERE id=?", (item['id'],))
                except Exception as exc:
                    message = str(exc) if isinstance(exc,Temporary) else 'Playlist-Operation fehlgeschlagen; erneut abgleichen.'
                    self.store.execute('UPDATE items SET reason=? WHERE id=?', (message,item['id']))
                    errors.append(message)
                    # Quota/network failure: don't keep spending quota on remaining mutations.
                    break
            remaining = self.store.query("SELECT id FROM items WHERE run_id=? AND status IN ('success','unavailable') AND move_status!='done'", (run_id,))
            self.store.execute('UPDATE runs SET archive_status=?,error=? WHERE id=?', ('partial' if remaining else 'done',' '.join(errors),run_id))
        except Exception as exc:
            message = str(exc) if isinstance(exc,Temporary) else 'Archivierung fehlgeschlagen; Quelle bleibt geschützt.'
            self.store.execute("UPDATE runs SET archive_status='partial',error=? WHERE id=?", (message,run_id))
        finally:
            yt.close()
            self.store.release()


def clean_prose(text):
    # The model never controls clickable URLs; only independently verified resources do.
    text = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'(?:https?://|www\.)\S+', '', text)
    text = re.sub(r'<[^>]*>', '', text)
    return re.sub(r'(?i)\b(?:zeitmarke|timestamp)\s*\d+:\d+(?::\d+)?\b', '', text).strip()


def urlsplit_name(url):
    from urllib.parse import urlsplit
    p = urlsplit(url)
    return (p.path.strip('/') if p.hostname == 'github.com' else p.hostname) or 'Ressource'
