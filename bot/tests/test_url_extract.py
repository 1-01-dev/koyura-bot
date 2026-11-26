import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from handlers.url_flow import extract_urls


def test_simple_url():
    urls = extract_urls("https://www.youtube.com/watch?v=KOYURA00001")
    assert len(urls) == 1
    assert "youtube.com" in urls[0]


def test_url_in_text():
    urls = extract_urls("check this out https://www.tiktok.com/@user/video/123 cool right?")
    assert len(urls) == 1
    assert "tiktok.com" in urls[0]


def test_multiple_urls():
    text = "https://youtube.com/watch?v=abc and also https://tiktok.com/@user/video/456"
    urls = extract_urls(text)
    assert len(urls) == 2


def test_no_url():
    urls = extract_urls("just some text without links")
    assert len(urls) == 0


def test_short_url():
    urls = extract_urls("https://youtu.be/KOYURA00001")
    assert len(urls) == 1


def test_instagram_reel():
    urls = extract_urls("https://www.instagram.com/reel/ABC123/")
    assert len(urls) == 1
    assert "instagram.com" in urls[0]


def test_twitter_url():
    urls = extract_urls("https://x.com/user/status/123456789")
    assert len(urls) == 1


def test_reddit_url():
    urls = extract_urls("https://www.reddit.com/r/videos/comments/abc123/test/")
    assert len(urls) == 1


def test_url_with_query_params():
    urls = extract_urls("https://www.youtube.com/watch?v=abc&list=PLxyz&index=3")
    assert len(urls) == 1
    assert "list=PLxyz" in urls[0]
