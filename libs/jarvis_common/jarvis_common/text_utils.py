"""Shared text utilities for author name matching."""


def normalize_author_name(name: str) -> str:
    """Lowercase, strip dots, collapse whitespace."""
    return " ".join(name.lower().replace(".", "").split())


def author_matches(tracked: str, candidate: str) -> bool:
    """Check if a candidate author name matches a tracked name.

    Handles exact match and last-name + first-initial match.
    """
    t = normalize_author_name(tracked)
    c = normalize_author_name(candidate)
    if t == c:
        return True
    # Last name + first initial match
    t_parts = t.split()
    c_parts = c.split()
    if len(t_parts) >= 2 and len(c_parts) >= 2:
        if t_parts[-1] == c_parts[-1] and t_parts[0][0] == c_parts[0][0]:
            return True
    return False
