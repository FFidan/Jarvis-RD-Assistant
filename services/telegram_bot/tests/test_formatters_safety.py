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
    """truncate must never cut inside — or leave open — an HTML tag.

    Telegram's HTML parse mode rejects a message containing a partially-cut
    tag (``<a hre``) or entity (``&amp``) with a 400 'can't parse entities'.
    ``truncate`` backs the cut up to just before the split tag/entity, and
    also closes (innermost-first) any spanning tag that is still open once
    the cut lands — so the result is always self-contained valid HTML and
    callers don't need to re-balance it themselves.

    All tests use ``max_length=120`` → effective cut at 20 chars
    (``limit = max_length - TRUNCATION_HEADROOM``).
    """

    def test_backs_up_before_partially_cut_opening_tag(self) -> None:
        # Cut at 20 lands two chars into '<a href="...">'.
        text = "x" * 18 + '<a href="http://e.com">y</a>'
        result = truncate(text, max_length=120)
        assert result == "x" * 18 + "\n\n<i>... (truncated)</i>"

    def test_backs_up_before_partially_cut_closing_tag(self) -> None:
        # Cut at 20 lands inside '</b>' → back up to before '<'. The opener
        # survives the backup, so truncate must close it before the marker.
        text = "<b>" + "x" * 15 + "</b>" + "y" * 10
        result = truncate(text, max_length=120)
        assert result == "<b>" + "x" * 15 + "</b>" + "\n\n<i>... (truncated)</i>"

    def test_keeps_complete_tag_just_before_cut(self) -> None:
        # A fully-formed tag before the cut must NOT be backed out, and the
        # opener it introduces must be closed before the marker.
        text = "x" * 10 + "<b>" + "y" * 20
        result = truncate(text, max_length=120)
        assert result == "x" * 10 + "<b>" + "y" * 7 + "</b>" + "\n\n<i>... (truncated)</i>"

    def test_backs_up_before_partially_cut_entity(self) -> None:
        # Cut at 20 lands inside '&amp;' → back up to before '&'.
        text = "x" * 17 + "&amp;" + "y" * 10
        result = truncate(text, max_length=120)
        assert result == "x" * 17 + "\n\n<i>... (truncated)</i>"

    def test_text_within_limit_is_unchanged(self) -> None:
        assert truncate("<b>short</b>") == "<b>short</b>"

    def test_closes_tag_opened_before_cut_with_content_past_it(self) -> None:
        # '<b>' opens well before the cut, content runs past it, and the
        # surviving 20-char span never reaches a closer.
        text = "<b>" + "x" * 40
        result = truncate(text, max_length=120)
        assert result == "<b>" + "x" * 17 + "</b>" + "\n\n<i>... (truncated)</i>"

    def test_closes_nested_tags_innermost_first(self) -> None:
        # '<b><i>' both open before the cut and neither closes — closers
        # must come out LIFO: '</i></b>', not '</b></i>'.
        text = "<b><i>" + "x" * 40
        result = truncate(text, max_length=120)
        assert result == "<b><i>" + "x" * 14 + "</i></b>" + "\n\n<i>... (truncated)</i>"

    def test_closes_open_anchor_tag_by_name_not_full_string(self) -> None:
        # An '<a href="...">' anchor open at the cut must be closed with
        # '</a>' — the tag NAME, not the full opening-tag string. The anchor
        # itself is 23 chars, wider than the other tests' 20-char cut, so
        # this case uses max_length=140 (limit=40) to keep it intact.
        anchor = '<a href="http://e.com">'
        text = anchor + "y" * 30
        result = truncate(text, max_length=140)
        assert result == anchor + "y" * 17 + "</a>" + "\n\n<i>... (truncated)</i>"

    def test_no_spurious_closer_for_already_balanced_tag(self) -> None:
        # '<b>done</b>' closes before the cut; the long filler after it is
        # what gets cut. No extra '</b>' should be appended.
        text = "<b>done</b>" + "y" * 20
        result = truncate(text, max_length=120)
        assert result == "<b>done</b>" + "y" * 9 + "\n\n<i>... (truncated)</i>"
