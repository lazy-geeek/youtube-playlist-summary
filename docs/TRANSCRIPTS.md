# Transkriptzugang und verbleibende Machbarkeitsprüfung

**Vor dem produktiven Einsatz muss die Transkriptquelle mit echten Videos aus der eigenen Playlist auf der tatsächlichen Hosting-Umgebung geprüft werden.**

Google OAuth erlaubt den Zugriff auf private Playlist-Einträge. Es verleiht keine Bearbeitungsrechte an fremden Videos. Die offizielle Methode `captions.download` erfordert Bearbeitungsrechte; ein 403 ist deshalb kein Nachweis fehlender Untertitel.

Implementierte Wege:

- **Apify / Starvibe**: `APIFY_API_TOKEN` und optional `APIFY_TRANSCRIPT_ACTOR=starvibe/youtube-video-transcript` konfigurieren, im Setup Apify auswählen. Vorhandene Untertitel werden über den Actor abgerufen. 120 Sekunden Laufzeitlimit und 0,02 USD Kostenlimit pro Video; keine automatische Wiederholung. Erfolgreiche Transkripte werden unter `DATA_DIR/apify-transcripts` wiederverwendet. Fremde Actor-IDs benötigen einen eigenen Adapter. Providerfehler oder fehlende Ergebnisse bleiben vorübergehende Fehler.

1. **Autorisiert importierte Transkripte** (Standard): UTF-8-Dateien unter `DATA_DIR/transcripts/VIDEO_ID.txt` (im Docker-Container standardmäßig `/app/data/transcripts/VIDEO_ID.txt`). Bereits vorhandene, zulässig bezogene Untertitel verwenden. Fehlende Dateien und zu kurze Texte werden als vorübergehende Fehler klassifiziert; die App kann daraus kein sicheres Fehlen von Untertiteln ableiten.
2. **Experimenteller öffentlicher Untertitelabruf**: `youtube-transcript-api` 1.2.x. Im Setup explizit auswählbar, kein Download von Audio, keine Anmeldung mit YouTube-Cookies, keine Proxies oder Umgehung von Sperren. Dieser Zugriff ist keine offizielle YouTube-API, beweist keine Nutzungsberechtigung und ist ohne Prüfung des konkreten Einsatzes nicht als zulässiger Produktionsweg bestätigt. Das Projekt dokumentiert IP-Sperren insbesondere bei Hosting-Anbietern. Private, altersbeschränkte oder loginpflichtige Videos werden nicht zuverlässig unterstützt.

Nur die spezifischen Untertitelquellen-Antworten `TranscriptsDisabled` / `NoTranscriptFound` beziehungsweise eine erfolgreiche leere Spurliste werden als nicht auswertbar eingestuft. Netzwerk-, Parsing-, Zugriffs- und Sperrfehler bleiben vorübergehend; fehlende Metadaten sind ebenfalls kein Beweis fehlender Transkripte. Untertitel mit weniger als 40 Wörtern bleiben zur manuellen Prüfung vorübergehend fehlgeschlagen. Eine inhaltliche Modellantwort ohne im Transkript enthaltene Belegzitate gilt als Modellfehler und bleibt zur Wiederholung in der Quelle.

## Erster echter Test

Google OAuth und die drei Playlist-IDs konfigurieren. Zugriffsrechte auf alle drei Playlists werden geprüft; alle müssen dem verbundenen YouTube-Kanal gehören. Im Setup „Transkriptzugang testen“ klicken. Bis zu drei echte Quell-Videos werden geprüft und Ergebnisse mit Video-ID, Herkunft, Wortzahl oder Fehlergrund gespeichert. Kein Text wird aus Titel oder Beschreibung als Vollzusammenfassung ersatzweise erzeugt, keine Playlist verändert. Mindestens ein erfolgreicher Transkriptabruf ist Voraussetzung für einen Berichtstart. Ein geänderter Quell-Playlist-/Transkriptzugangs-/Google-Account-Kontext benötigt einen neuen Test.

Die Prüfung muss **auf dem finalen Server** stattfinden, weil der Abruf auf einem lokalen Gerät erfolgreich sein kann, während die Hosting-IP gesperrt wird. Für die Abnahme zusätzlich mehrere reale Videos unterschiedlicher Kanäle prüfen, einen vollständigen Modellbericht lesen und Quellenzuordnung sowie Ressourcennachweise kontrollieren. Ein einzelner positiver Probeabruf ist keine Garantie für die Zuverlässigkeit des Produktionsbetriebs.

Falls öffentliche Untertitel dort unzulässig oder unzuverlässig sind, verbleiben ein rechtmäßig nutzbarer lizenzierter Transkriptanbieter (zusätzliche Integration nötig) oder autorisiert bereitgestellte Untertitelimporte. Ohne nutzbare Quelle bleibt die automatische Auswertung blockiert. Audio-Transkription ist ausgeschlossen.

## Primärquellen

- https://developers.google.com/youtube/v3/docs/captions/download
- https://github.com/jdepoix/youtube-transcript-api (Abruf, Fehlerfälle und Hosting-IP-Sperren)
