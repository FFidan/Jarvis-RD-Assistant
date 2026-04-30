from paper_ingestion.queries.predicates import IS_ARCHIVED_SQL, IS_DISMISSED_SQL, IS_SAVED_SQL


def test_predicates_match_expected_strings():
    # NULL-safe form: `IS NOT DISTINCT FROM` evaluates FALSE (not NULL) when
    # pus.status IS NULL, so `NOT IS_ARCHIVED_SQL` won't drop never-touched papers.
    assert IS_ARCHIVED_SQL == (
        "(COALESCE(pus.archived, FALSE) OR pus.status IS NOT DISTINCT FROM 'archived')"
    )
    assert IS_DISMISSED_SQL == "COALESCE(pus.dismissed, FALSE)"
    assert IS_SAVED_SQL == "COALESCE(pus.saved, FALSE)"
