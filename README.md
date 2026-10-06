# Playlist Briefing

Eine selbst gehostete Web-App, die eine YouTube-Playlist auf manuellen Start auswertet und deutschsprachige Zusammenfassungen pro Video erstellt. Jedes Video erhält eine eigene deutsche Zusammenfassung seiner wesentlichen Aspekte aus Titel und vollständigem Transkript, mit eigenem Lesestatus.

## Funktionen

- Google OAuth für den Zugriff auf private Playlists.
- Eine feste Quell-Playlist sowie zwei bereits vorhandene Ziel-Playlists.
- Transkriptanalyse mit konfigurierbarem OpenRouter-Modell.
- Gemeinsame Auswertung thematischer Überschneidungen über mehrere Videos.
- Geprüfte Ressourcenlinks und klar gekennzeichnete Unsicherheit.
- Einzelne Leseabschnitte, dauerhafte Historie und vollständiger Markdown-Export.
- Automatische Archivierung nach erfolgreicher Erstellung und Speicherung des Briefings; der Lesestatus bleibt unabhängig.
- Fortsetzbare Playlist-Operationen mit gespeichertem Fortschritt.
- Benutzername/Passwort-Schutz, serverseitig verschlüsselte Google-Tokens und CSRF-Schutz.

## Ablauf

Ein Durchlauf speichert die beim Start gelesenen Playlist-Einträge einschließlich ihrer **Playlist-Item-IDs**. Erfolgreich ausgewertete Videos fließen in einen gemeinsamen Bericht ein. Sicher fehlende Untertitel und vorübergehende Fehler werden getrennt behandelt; Titel und Beschreibungen ersetzen kein Transkript.

Die Themenabschnitte können einzeln als gelesen markiert werden und verschwinden dann aus der offenen Leseliste. Sobald alle Abschnitte gelesen sind, gilt der Lesevorgang als abgeschlossen. Der vollständige Bericht bleibt in der Historie und im Markdown-Export erhalten.

Erst **Gelesen und archivieren** verändert die Playlists: erfolgreich ausgewertete Videos kommen ins Archiv, sicher nicht auswertbare Videos in die entsprechende dritte Playlist. Vorübergehend fehlgeschlagene Videos bleiben für einen späteren Versuch in der Quelle. Es werden ausschließlich Playlist-Einträge entfernt, niemals YouTube-Videos gelöscht.

## Voraussetzungen

- Python 3.12 oder neuer und [uv](https://docs.astral.sh/uv/).
- Google-Cloud-Projekt mit aktivierter YouTube Data API v3.
- Google-OAuth-Client vom Typ Webanwendung.
- Drei unterschiedliche Playlists, die dem angemeldeten YouTube-Kanal gehören.
- OpenRouter-API-Schlüssel und eine passende Modellkennung.
- Ein zulässig nutzbarer und auf der Hosting-Umgebung getesteter Transkriptzugang.

**Transkriptzugang ist eine Voraussetzung, keine zugesicherte Eigenschaft jedes Videos oder Servers.** Die offizielle YouTube-Methode `captions.download` erfordert Bearbeitungsrechte am Video. OAuth für eine private Playlist gewährt diese Rechte an fremden Videos nicht. Details und Grenzen stehen in [docs/TRANSCRIPTS.md](docs/TRANSCRIPTS.md).

## Lokal starten

```sh
uv sync
uv run python scripts/init_env.py
```

Die erzeugte `.env` lokal bearbeiten, insbesondere den OpenRouter-Schlüssel setzen. Anschließend:

```sh
uv run uvicorn app.main:app --host 127.0.0.1 --port 8765 --workers 1 --no-access-log
```

Die App ist unter `http://localhost:8765` erreichbar. Benutzername und Passwort stehen in der lokalen `.env`.

| Variable | Zweck |
| --- | --- |
| `APP_URL` | Öffentliche Basis-URL ohne abschließenden Schrägstrich; lokal `http://localhost:8765` |
| `APP_USERNAME` | Benutzername für den HTTP-Passwortschutz; Standard `owner` |
| `APP_PASSWORD` | Starkes Passwort mit mindestens 16 Zeichen |
| `ENCRYPTION_KEY` | Fernet-Schlüssel für die verschlüsselte Speicherung von Google-Zugangsdaten |
| `DATA_DIR` | Persistentes Datenverzeichnis; lokal `./data` |
| `OPENROUTER_API_KEY` | OpenRouter-Schlüssel, ausschließlich über die Serverumgebung gesetzt |
| `APIFY_API_TOKEN` | Apify-Key, ausschließlich über die Serverumgebung |
| `APIFY_TRANSCRIPT_LANGUAGE` | Bevorzugte Untertitelsprache; Standard `en` |
| `APIFY_TRANSCRIPT_ACTOR` | Actor-ID; unterstützt: `starvibe/youtube-video-transcript` |
| `SEARXNG_URL` | Basis-URL der eigenen SearXNG-Instanz; im Setup Suchdienst `searxng` wählen, JSON-API erforderlich |
| `GOOGLE_ALLOWED_EMAIL` | Optionale Vorbelegung der berechtigten Google-Adresse im Setup |

`.env`, Datenbanken, Berichte und lokal erzeugte Zugangsdaten gehören nicht ins Repository. `ENCRYPTION_KEY` muss dauerhaft gesichert werden: Ein Austausch macht vorhandene verschlüsselte Tokens unlesbar.

## Google und Playlists einrichten

1. YouTube Data API v3 im eigenen Google-Cloud-Projekt aktivieren und den OAuth-Zustimmungsbildschirm konfigurieren. Im externen Testbetrieb die berechtigte Google-Adresse als Testnutzer eintragen.
2. OAuth-Client vom Typ **Webanwendung** anlegen. Als Redirect URI lokal `http://localhost:8765/oauth/callback` oder in Produktion `https://briefing.example.com/oauth/callback` verwenden. Die Produktionsadresse ist durch die eigene Domain zu ersetzen und muss exakt zu `APP_URL` passen.
3. Im geschützten App-Setup die drei Playlist-IDs, Client-ID, Client-Secret, berechtigte Google-Adresse und OpenRouter-Modellkennung speichern. Nach dem ersten Durchlauf sind die Playlist-IDs fest.
4. Google anmelden. Angefordert werden `openid`, `email` und `youtube.force-ssl`. Es werden keine Playlists automatisch erstellt.
5. Transkriptquelle auswählen und **Transkriptzugang testen** ausführen. Mindestens ein erfolgreicher Probeabruf ist Voraussetzung für einen Berichtstart.

Gespeicherte Secrets werden nicht erneut in Formularen angezeigt. Der OpenRouter-Schlüssel hat kein Eingabefeld in der App.

## Docker und Dokku

Das Dockerfile verwendet Python 3.12, den Dependency-Lock und einen nicht privilegierten Benutzer mit UID 10001. Der Webprozess hört auf Port 8000. Für die Produktion:

- HTTPS und die korrekte `APP_URL` konfigurieren.
- Genau **einen** App-Prozess betreiben.
- Ein beschreibbares persistentes Verzeichnis nach `/app/data` mounten; Eigentümer UID/GID 10001, Modus 0700.
- Passwort und Verschlüsselungsschlüssel serverseitig setzen.
- Datenbank, Transkriptimporte und Verschlüsselungsschlüssel sichern.

Die SQLite-Datenbank speichert Berichte, Abschnitte, Lesestand, Snapshots und Operationsfortschritt. Ein exklusives Prozess-Lock verhindert parallele Instanzen. Bei Dokku muss deshalb **Stop-before-start** verwendet werden; rollende Deployments mit gleichzeitig laufenden Containern sind nicht unterstützt. Während eines Deployments entsteht eine kurze Unterbrechung. Aktive Arbeit vor einem Deployment beenden lassen.

### Optionale Dokku-Hilfsskripte

Benötigt werden ein eigener Dokku-Host sowie die Plugins `http-auth` und `letsencrypt`. Die Skripte enthalten keine vorkonfigurierte Produktionsdomain oder SSH-Verbindung. Folgendes Beispiel muss auf die eigene Umgebung angepasst werden:

```sh
uv run python scripts/prepare_deploy.py --url https://briefing.example.com
scp scripts/bootstrap_server.py data/playlist-briefing.tar.gz root@your-dokku-host:/tmp/
ssh root@your-dokku-host \
  'python3 /tmp/bootstrap_server.py --app playlist-briefing --domain briefing.example.com' \
  < data/deploy-secrets.json
ssh root@your-dokku-host \
  'cd /tmp && dokku git:from-archive playlist-briefing file:///tmp/playlist-briefing.tar.gz'
ssh root@your-dokku-host 'dokku letsencrypt:enable playlist-briefing'
```

Das erste Skript erzeugt lokal ein Deployment-Archiv und geschützte, ignorierte Zugangsdaten. Das Server-Skript legt ausschließlich die benannte App an und verweigert das Überschreiben einer fremden App. **Eine bereits eingerichtete Datenbank darf nicht mit einem neuen Verschlüsselungsschlüssel konfiguriert werden.** Für spätere Deployments nur das Archiv übertragen und bauen.

`OPENROUTER_API_KEY` zusätzlich über die eigene Secret-Verwaltung oder Dokku-Konfiguration setzen. Den Schlüssel nicht als Klartext in eine Shell-History schreiben. Bei Verwendung des Dokku-Passwortschutzes müssen Benutzername und Passwort dort mit `APP_USERNAME` und `APP_PASSWORD` übereinstimmen.

Ein Smoke-Test ist nach dem Deployment möglich:

```sh
uv run python scripts/verify_live.py --url https://briefing.example.com
```

Der Test verwendet lokale Zugangsdaten und gibt sie nicht aus. Er verändert keine Playlists.

## Ressourcenlinks und Modellgrenzen

Transkripte, Beschreibungen und bis zu 200 relevante veröffentlichte Top-Level-Kommentare liefern Linkkandidaten. Nur Kommentare mit derselben Kanal-ID wie das Video werden als Erstellerkommentare verwendet. Unzugängliche Kommentare unterbrechen den Durchlauf nicht.

Fehlende URLs werden mit dem OpenRouter-Servertool `openrouter:web_search` recherchiert. Die Antwort muss einen tatsächlich ausgeführten Suchaufruf nachweisen. Anschließend werden Zieladresse und Projektbezug geprüft. Unsichere Kandidaten bleiben als Namen mit Unsicherheitshinweis stehen. Modellgenerierte URLs in der Berichtprosa werden entfernt.

Interne Provenienz speichert Befunde, Video-IDs, wörtliche Belegzitate und Recherchekonfiguration. Belegzitate und die Abdeckung aller erfolgreichen Videos werden geprüft. Die semantische Richtigkeit bleibt eine Modellgrenze; ein echter Bericht muss vor dem produktiven Einsatz inhaltlich geprüft werden. Nicht jedes Modell unterstützt die benötigten Tool- und JSON-Modi.

## Archivierung und Fehlerfälle

Ein vorhandener Ziel-Eintrag wird erkannt oder ein neuer angelegt. Erst nach erneuter Zielprüfung wird der konkrete Quell-Playlist-Eintrag gelöscht. Fortschritt wird gespeichert; Quota- oder Netzwerkfehler lassen sich bei einem späteren Versuch abgleichen.

YouTube und SQLite bilden keine atomare Transaktion. Falls das Ergebnis eines Hinzufügens unklar ist und kein Ziel-Eintrag gefunden wird, fügt die App nicht blind erneut hinzu und löscht nichts aus der Quelle. Bei dauerhaft ungeklärtem Zustand ist eine manuelle Prüfung erforderlich. Bestehende fremde Duplikate werden nicht automatisch aufgeräumt.

## Sicherung

Eine aktive SQLite/WAL-Datenbank mit der SQLite-Backup-API konsistent sichern. Zusätzlich den unveränderten Verschlüsselungsschlüssel und autorisierte Transkriptimporte sichern. Nach einer Wiederherstellung gelten weiterhin Einzelprozessbetrieb und korrekte Dateirechte. Das Projekt richtet keinen automatischen Backup-Zeitplan ein.

## Tests

```sh
uv run pytest -q
```

Die Tests prüfen Zugriffsschutz, CSRF, Secret-Ausgabe, HTML-Sanitizing, gespeicherten Lesestand, Markdown-Export, thematische Zusammenführung und fortsetzbare Playlist-Operationen. Sie ersetzen keine Integrationstests mit eigenen Google- und OpenRouter-Zugängen auf der tatsächlichen Hosting-Umgebung.

## Technische Quellen

- [Google OAuth](https://developers.google.com/youtube/v3/guides/authentication)
- [YouTube Captions Download](https://developers.google.com/youtube/v3/docs/captions/download)
- [Playlist-Einträge hinzufügen](https://developers.google.com/youtube/v3/docs/playlistItems/insert) und [entfernen](https://developers.google.com/youtube/v3/docs/playlistItems/delete)
- [Kommentare](https://developers.google.com/youtube/v3/docs/commentThreads/list) und [Quota](https://developers.google.com/youtube/v3/determine_quota_cost)
- [OpenRouter Websuche](https://openrouter.ai/docs/guides/features/server-tools/web-search)

Bei Auswahl von SearXNG sucht die App über dessen `/search?format=json`-API. Nur URLs aus den tatsächlichen Treffern werden als Kandidaten zugelassen und anschließend unabhängig geprüft. Es gibt keinen automatischen Rückfall auf kostenpflichtige OpenRouter-Websuche. OpenRouter bleibt für die Textauswertung zuständig.

Im Setup kann die maximale Videoanzahl pro Lauf gesetzt werden. Leer bedeutet alle Videos. Die N ältesten Videos nach Veröffentlichungsdatum bilden den Snapshot (fehlendes Veröffentlichungsdatum zuletzt); weitere Einträge bleiben in der Quelle. Das Limit wird im Lauf gespeichert und begrenzt auch die spätere Archivierung auf dessen Snapshot.
