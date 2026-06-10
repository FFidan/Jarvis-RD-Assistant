"""Tests for telegram_bot hardening fixes (H-3, M-5, M-6, M12b)."""

from telegram_bot.formatters import confidence_badge, safe_url, sanitize_user_input, truncate

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


class TestTruncateTagAware:
    """M12b: truncate must never cut inside an HTML tag or entity.

    Telegram's HTML parse mode rejects a message containing a partially-cut
    tag (``<a hre``) or entity (``&amp``) with a 400 'can't parse entities'.
    ``truncate`` backs the cut up to just before the split tag/entity.  Note
    the contract is *no mid-tag/mid-entity cut* — re-balancing tags left open
    by the cut is the caller's job (see paper_digest._send_chunked).

    All tests use ``max_length=120`` → effective cut at 20 chars
    (``limit = max_length - TRUNCATION_HEADROOM``).
    """

    def test_backs_up_before_partially_cut_opening_tag(self) -> None:
        # Cut at 20 lands two chars into '<a href="...">'.
        text = "x" * 18 + '<a href="http://e.com">y</a>'
        result = truncate(text, max_length=120)
        assert result == "x" * 18 + "\n\n<i>... (truncated)</i>"

    def test_backs_up_before_partially_cut_closing_tag(self) -> None:
        # Cut at 20 lands inside '</b>' → back up to before '<'.
        text = "<b>" + "x" * 15 + "</b>" + "y" * 10
        result = truncate(text, max_length=120)
        assert result == "<b>" + "x" * 15 + "\n\n<i>... (truncated)</i>"

    def test_keeps_complete_tag_just_before_cut(self) -> None:
        # A fully-formed tag before the cut must NOT be backed out.
        text = "x" * 10 + "<b>" + "y" * 20
        result = truncate(text, max_length=120)
        assert result == "x" * 10 + "<b>" + "y" * 7 + "\n\n<i>... (truncated)</i>"

    def test_backs_up_before_partially_cut_entity(self) -> None:
        # Cut at 20 lands inside '&amp;' → back up to before '&'.
        text = "x" * 17 + "&amp;" + "y" * 10
        result = truncate(text, max_length=120)
        assert result == "x" * 17 + "\n\n<i>... (truncated)</i>"

    def test_text_within_limit_is_unchanged(self) -> None:
        assert truncate("<b>short</b>") == "<b>short</b>"
