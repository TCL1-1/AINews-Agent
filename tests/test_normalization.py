from app.normalization import canonicalize_url, normalize_title, stable_hash


def test_canonicalize_url_removes_tracking_and_fragment() -> None:
    value = canonicalize_url(
        "HTTPS://Example.COM/news/?utm_source=x&b=2&a=1#section"
    )
    assert value == "https://example.com/news?a=1&b=2"


def test_normalized_title_has_stable_hash() -> None:
    first = normalize_title("  New AI-Chip!  ")
    second = normalize_title("new ai chip")
    assert first == second
    assert stable_hash(first) == stable_hash(second)

