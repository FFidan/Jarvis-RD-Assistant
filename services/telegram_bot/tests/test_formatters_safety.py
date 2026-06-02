"""Tests for telegram_bot hardening fixes (H-3, M-5, M-6)."""

from telegram_bot.formatters import confidence_badge, safe_url, sanitize_user_input

_BIDI = "‮"  # RIGHT-TO-LEFT OVERRIDE


class TestSanitizeUserInput:
    """sanitize_user_input must strip BIDI BEFORE truncating.

    Truncate-first lets BIDI/zero-width chars consume the length budget, so
    real content past the boundary is silently dropped even though those
    invisible chars get stripped afterwards. Stripping first means the cap
    applies to clean content only.
    """

    def test_strips_bidi_padding_before_applying_length_cap(self) -> None:
        max_len = 10
        # BIDI padding at the front eats into the budget under truncate-first,
        # dropping the trailing real char. Strip-first keeps all 10 real chars.
        text = _BIDI + "A" * 10
        result = sanitize_user_input(text, max_len)
        assert _BIDI not in result
        assert result == "A" * 10

    def test_no_bidi_survives_at_truncation_boundary(self) -> None:
        max_len = 5
        text = "ABCD" + _BIDI + "EFGH"
        result = sanitize_user_input(text, max_len)
        assert _BIDI not in result
        assert result == "ABCDE"

    def test_caps_clean_content_to_max_len(self) -> None:
        assert sanitize_user_input("A" * 100, 5) == "AAAAA"


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
