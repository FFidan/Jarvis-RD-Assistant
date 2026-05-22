"""Tests for telegram_bot hardening fixes (H-3, M-5, M-6)."""

from telegram_bot.formatters import confidence_badge, safe_url


class TestSafeUrl:
    """H-3: safe_url blocks dangerous URI schemes."""

    def test_blocks_javascript_uri(self) -> None:
        assert safe_url("javascript:alert(1)") == "#"

    def test_blocks_data_uri(self) -> None:
        assert safe_url("data:text/html,<h1>hi</h1>") == "#"

    def test_allows_https(self) -> None:
        url = "https://arxiv.org/abs/1234"
        assert safe_url(url) == url

    def test_allows_http(self) -> None:
        url = "http://example.com"
        assert safe_url(url) == url

    def test_allows_empty_string(self) -> None:
        assert safe_url("") == ""


class TestConfidenceBadge:
    """M-6: confidence_badge escapes unknown values."""

    def test_known_high(self) -> None:
        assert confidence_badge("HIGH") == "\U0001f7e2 HIGH"

    def test_unknown_value_is_escaped(self) -> None:
        result = confidence_badge("<script>xss</script>")
        assert "<script>" not in result
        assert "&lt;script&gt;" in result
