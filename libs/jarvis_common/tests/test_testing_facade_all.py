"""Guard: facade re-export + testing_db helper contracts."""


def test_seed_helpers_not_in_testing_all() -> None:
    """_seed_user and _seed_resources are internal; must not appear in __all__."""
    import jarvis_common.testing as t

    assert "_seed_user" not in t.__all__
    assert "_seed_resources" not in t.__all__


def test_spin_pg_container_helper_exists() -> None:
    """Private helper must exist to eliminate duplicated container setup."""
    from jarvis_common import testing_db

    assert hasattr(testing_db, "_spin_pg_container"), (
        "_spin_pg_container must exist to centralize container setup"
    )
