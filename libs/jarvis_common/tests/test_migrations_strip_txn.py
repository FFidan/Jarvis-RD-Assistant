"""Regression tests for _strip_outer_transaction_control comment-awareness.

Migration 080 was the first migration to put a literal ``$$`` token inside a
``--`` line comment. The dollar-quote state machine had no comment awareness,
so that single unbalanced ``$$`` inverted the in_dollar polarity for the rest
of the file. The real ``DO $$ ... $$`` pre-flight block was then scanned as if
outside a dollar block, and its standalone ``BEGIN`` line was stripped as
"outer transaction control" — leaving ``DO $$ DECLARE ... FOREACH`` with no
``BEGIN``, which Postgres rejects with "syntax error at or near FOREACH".
"""

from jarvis_common.migrations import _strip_outer_transaction_control


def test_dollar_in_line_comment_does_not_invert_dollar_state() -> None:
    sql = (
        "-- DO $$ ... EXCEPTION WHEN duplicate_object guard (mig 051 pattern).\n"
        "DO $$\n"
        "DECLARE\n"
        "    t TEXT;\n"
        "BEGIN\n"
        "    FOREACH t IN ARRAY ARRAY['a', 'b'] LOOP\n"
        "        RAISE NOTICE '%', t;\n"
        "    END LOOP;\n"
        "END $$;\n"
    )
    out = _strip_outer_transaction_control(sql)
    # The standalone BEGIN inside the DO block must survive — it is PL/pgSQL,
    # not outer transaction control.
    assert "BEGIN\n    FOREACH" in out
    # All $$ delimiters (including the one in the comment) are preserved.
    assert out.count("$$") == sql.count("$$")


def test_outer_transaction_control_still_stripped() -> None:
    sql = "BEGIN;\nALTER TABLE t ADD COLUMN c INT;\nCOMMIT;\n"
    out = _strip_outer_transaction_control(sql)
    assert "ALTER TABLE t ADD COLUMN c INT;" in out
    assert "BEGIN;" not in out
    assert "COMMIT;" not in out


def test_begin_inside_do_block_without_comment_still_preserved() -> None:
    sql = "DO $$ BEGIN\n    PERFORM 1;\nEND $$;\n"
    out = _strip_outer_transaction_control(sql)
    assert "DO $$ BEGIN" in out
    assert "PERFORM 1;" in out
