# covers: FR-3, NFR-3
from app.analytics import AnalyticsRecorder, referrer_host, ua_family


def test_ua_family_classification():
    assert ua_family("Mozilla/5.0 Chrome/120 Safari/537") == "chrome"
    assert ua_family("Mozilla/5.0 Firefox/121") == "firefox"
    assert ua_family("curl/8.0") == "bot"
    assert ua_family(None) == "unknown"


def test_referrer_host():
    assert referrer_host("https://News.Example.com/a?b") == "news.example.com"
    assert referrer_host(None) is None


def test_ip_is_never_stored_raw(repo):
    rec = AnalyticsRecorder(repo, salt="s", batch_size=1000)
    h = rec.visitor_hash("203.0.113.9")
    assert "203.0.113.9" not in h and len(h) == 16
    assert h == rec.visitor_hash("203.0.113.9")


def test_batching_and_stats(service, clock):
    link = service.create("https://example.com")
    for ip in ["1.1.1.1", "1.1.1.1", "2.2.2.2"]:
        service.record_click(link.code, ip, "Chrome", "https://t.co/x")
    assert service.analytics.pending == 3          # buffered, not yet written
    s = service.stats(link.code)                   # stats flushes first
    assert s["total_clicks"] == 3
    assert s["unique_visitors"] == 2
    assert s["top_referrers"] == [("t.co", 3)]
    assert s["by_user_agent"] == {"chrome": 3}


def test_auto_flush_at_batch_size(repo):
    rec = AnalyticsRecorder(repo, salt="s", batch_size=2)
    from app.models import Link
    repo.create(Link("abc", "https://e.com", 0.0))
    rec.record("abc", "1.1.1.1", None, None)
    rec.record("abc", "1.1.1.2", None, None)
    assert rec.pending == 0
    assert repo.stats("abc")["total_clicks"] == 2
