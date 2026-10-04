import pytest
from app.providers import Transcripts, Temporary


def test_apify_transcript_cached_and_bound_to_video(tmp_path, monkeypatch):
    monkeypatch.setenv('APIFY_API_TOKEN', 'test-key')
    calls = []
    class Response:
        def raise_for_status(self): pass
        def json(self): return [{'video_id':'abcdefghijk', 'status':'success', 'transcript_text':'word ' * 50}]
    def post(url, **kw):
        calls.append(url)
        assert kw['headers']['Authorization'] == 'Bearer test-key'
        assert kw['params']['maxTotalChargeUsd'] == 0.02
        assert kw['json']['youtube_url'].endswith('abcdefghijk')
        return Response()
    monkeypatch.setattr('app.providers.httpx.post', post)
    provider = Transcripts('apify',tmp_path)
    assert len(provider.fetch('abcdefghijk')[0].split()) == 50
    assert provider.fetch('abcdefghijk')[1].startswith('apify:starvibe/')
    assert len(calls) == 1


def test_apify_wrong_video_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv('APIFY_API_TOKEN', 'test-key')
    class Response:
        def raise_for_status(self): pass
        def json(self): return [{'video_id':'wrong-video', 'status':'success', 'transcript_text':'word ' * 50}]
    monkeypatch.setattr('app.providers.httpx.post', lambda *a, **k: Response())
    with pytest.raises(Temporary): Transcripts('apify',tmp_path).fetch('abcdefghijk')
    assert not (tmp_path/'apify-transcripts').exists()


def test_apify_missing_key(tmp_path, monkeypatch):
    monkeypatch.delenv('APIFY_API_TOKEN',raising=False)
    with pytest.raises(Temporary,match='APIFY_API_TOKEN'): Transcripts('apify',tmp_path).fetch('abcdefghijk')
