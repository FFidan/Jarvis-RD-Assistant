"""Value validators for each config key type."""

from collections.abc import Callable
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

from apscheduler.triggers.cron import CronTrigger

__all__ = [
    "_PULSE_WEIGHT_KEYS",
    "_PULSE_REQUIRED_WEIGHT_KEYS",
    "_CONFIG_VALIDATORS",
    "_validate_cron",
    "_validate_pulse_weights",
    "_validate_positive_int",
    "_validate_bool",
    "_validate_optional_int",
    "_validate_l2_lambda",
    "_validate_lookback_days",
    "_validate_startup_grace_seconds",
    "_validate_nonempty_str",
    "_validate_library_type",
    "_validate_group_id",
    "_validate_zotero_cron",
    "_validate_langfuse_dashboard_url",
    "_validate_fsrs_retention",
    "_validate_fsrs_learning_steps",
]

_PULSE_WEIGHT_KEYS = frozenset(
    {
        "embedding",
        "topic",
        "llm_relevance",
        "llm_novelty",
        "author_bonus",
        "recency",
        "citation_pagerank",
        "citation_count",
        "citation_adamic_adar",
        "classifier",
    }
)
_PULSE_REQUIRED_WEIGHT_KEYS = frozenset(
    {"embedding", "topic", "llm_relevance", "llm_novelty", "author_bonus", "recency"}
)


# ---------------------------------------------------------------------------
# DRY hoist 3: shared cron validation body
# ---------------------------------------------------------------------------


def _validate_cron_base(v: Any, name: str) -> CronTrigger:
    """Shared cron validation: type-check and parse; return the trigger on success."""
    if not isinstance(v, str):
        raise ValueError(f"{name} must be a string")
    try:
        return CronTrigger.from_crontab(v)
    except Exception as exc:
        raise ValueError(f"invalid cron expression: {exc}") from exc


def _validate_cron(v: Any) -> None:
    trigger = _validate_cron_base(v, "pulse.cron")
    # Reject sub-hourly schedules — pulse runs are expensive; once per hour is the minimum.
    from datetime import datetime  # noqa: PLC0415

    base = datetime.now()
    t1 = trigger.get_next_fire_time(None, base)
    t2 = trigger.get_next_fire_time(t1, t1)
    if t1 is not None and t2 is not None and (t2 - t1) < timedelta(hours=1):
        raise ValueError("Pulse cron must fire no more than once per hour")


def _validate_zotero_cron(v: Any) -> None:
    if not isinstance(v, str):
        raise ValueError("zotero.poll_cron must be a string")
    _validate_cron_base(v, "zotero.poll_cron")


def _validate_pulse_weights(v: Any) -> None:
    if not isinstance(v, dict):
        raise ValueError("pulse.weights must be a dict")
    keys = set(v.keys())
    if not _PULSE_REQUIRED_WEIGHT_KEYS.issubset(keys) or not keys.issubset(_PULSE_WEIGHT_KEYS):
        raise ValueError(
            "pulse.weights must include the core keys and only known optional keys: "
            f"{sorted(_PULSE_WEIGHT_KEYS)}"
        )
    for k, val in v.items():
        if not isinstance(val, int | float) or isinstance(val, bool) or not (0 <= val <= 1):
            raise ValueError(f"pulse.weights.{k} must be a float between 0 and 1")


def _validate_positive_int(v: Any) -> None:
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        raise ValueError("value must be a positive integer")


def _validate_bool(v: Any) -> None:
    if not isinstance(v, bool):
        raise ValueError("value must be a boolean")


def _validate_optional_int(v: Any) -> None:
    if v is None:
        return
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError("value must be an integer or null")


# ---------------------------------------------------------------------------
# DRY hoist 1: numeric-range combinator
# ---------------------------------------------------------------------------


def _validate_numeric_range(
    v: Any,
    *,
    lo: float,
    hi: float,
    name: str,
    require_int: bool = False,
    exclusive_bounds: bool = False,
) -> None:
    """Validate that *v* is a number (or int) within [lo, hi] (or (lo, hi) if exclusive_bounds)."""
    if require_int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f"{name} must be an integer")
    else:
        if isinstance(v, bool) or not isinstance(v, int | float):
            raise ValueError(f"{name} must be a number")
    fv = float(v)
    if exclusive_bounds:
        if not (lo < fv < hi):
            raise ValueError(f"{name} must be between {lo} and {hi} (exclusive)")
    else:
        if not (lo <= fv <= hi):
            raise ValueError(f"{name} must be between {lo} and {hi}")


def _validate_l2_lambda(v: Any) -> None:
    """Validate pulse.l2_lambda — cosine-penalty multiplier for negative signals.

    Range [0, 2]: 0 disables the penalty, 1 = equal-weight, 2 = double-weight.
    Values >2 make the penalty dominate scoring, which is considered unsafe.
    """
    _validate_numeric_range(v, lo=0.0, hi=2.0, name="pulse.l2_lambda")


def _validate_lookback_days(v: Any) -> None:
    """Validate pulse.lookback_days — discovery window in days, [1, 90]."""
    _validate_numeric_range(v, lo=1, hi=90, name="pulse.lookback_days", require_int=True)


def _validate_startup_grace_seconds(v: Any) -> None:
    """Validate pulse.startup_grace_seconds — warmup pause, [0, 300]."""
    _validate_numeric_range(v, lo=0.0, hi=300.0, name="pulse.startup_grace_seconds")


def _validate_nonempty_str(v: Any) -> None:
    if not isinstance(v, str) or not v.strip():
        raise ValueError("value must be a non-empty string")


def _validate_library_type(v: Any) -> None:
    if v not in ("user", "group"):
        raise ValueError("zotero.library_type must be 'user' or 'group'")


def _validate_group_id(v: Any) -> None:
    """Validate zotero.group_id — positive integer or null.

    ``null`` is allowed so users can clear the field when switching back to
    a personal library.  When ``library_type`` is ``"group"`` the backend
    requires a non-null positive integer, but that cross-field validation is
    enforced by :class:`~paper_ingestion.integrations.zotero_client.ZoteroClient`
    at construction time.
    """
    if v is None:
        return
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise ValueError("zotero.group_id must be a positive integer or null")


def _validate_langfuse_dashboard_url(v: Any) -> None:
    """Validate observability.langfuse_dashboard_url.

    Accepts an empty string (clears the link), any ``https://`` URL with a
    host, or an ``http://`` URL whose host is loopback (``localhost`` /
    ``127.0.0.1``) so a local-dev Langfuse reachable only over plain HTTP
    still works. Everything else is rejected — the value is rendered as a
    user-facing link, so non-http(s) schemes (e.g. ``javascript:``) and
    plain-HTTP non-loopback hosts must not be storable.
    """
    if not isinstance(v, str):
        raise ValueError("observability.langfuse_dashboard_url must be a string")
    if v == "":
        return
    parsed = urlparse(v)
    if parsed.scheme == "https" and parsed.netloc:
        return
    if parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1"):
        return
    raise ValueError(
        "observability.langfuse_dashboard_url must be empty, an https:// URL, "
        "or an http://localhost / http://127.0.0.1 URL"
    )


def _validate_fsrs_retention(v: Any) -> None:
    """Validate fsrs.desired_retention — float in (0, 1) exclusive."""
    _validate_numeric_range(v, lo=0.0, hi=1.0, name="fsrs.desired_retention", exclusive_bounds=True)


def _validate_fsrs_learning_steps(v: Any) -> None:
    """Validate fsrs.learning_steps — list of exactly 2 positive integers (minutes)."""
    if not isinstance(v, list):
        raise ValueError("fsrs.learning_steps must be a list")
    if len(v) != 2:
        raise ValueError("fsrs.learning_steps must have exactly 2 elements")
    for i, step in enumerate(v):
        if isinstance(step, bool) or not isinstance(step, int) or step <= 0:
            raise ValueError(f"fsrs.learning_steps[{i}] must be a positive integer (minutes)")


_CONFIG_VALIDATORS: dict[str, Callable[[Any], None]] = {
    # FSRS
    "fsrs.desired_retention": _validate_fsrs_retention,
    "fsrs.learning_steps": _validate_fsrs_learning_steps,
    "pulse.cron": _validate_cron,
    "pulse.weights": _validate_pulse_weights,
    "pulse.deck_size": _validate_positive_int,
    "pulse.stage2_top_k": _validate_positive_int,
    "pulse.l2_lambda": _validate_l2_lambda,
    "pulse.lookback_days": _validate_lookback_days,
    "pulse.startup_grace_seconds": _validate_startup_grace_seconds,
    "pulse.enabled": _validate_bool,
    "setup.completed": _validate_bool,
    "telegram.owner_chat_id": _validate_optional_int,
    # LLM model role assignments
    "llm.smart_model": _validate_nonempty_str,
    "llm.fast_model": _validate_nonempty_str,
    "llm.embed_model": _validate_nonempty_str,
    # Zotero
    "zotero.api_key": _validate_nonempty_str,
    "zotero.user_id": _validate_nonempty_str,
    "zotero.library_type": _validate_library_type,
    "zotero.group_id": _validate_group_id,
    "zotero.poll_enabled": _validate_bool,
    "zotero.poll_cron": _validate_zotero_cron,
    "zotero.auto_push_on_star": _validate_bool,
    # Observability
    "observability.langfuse_dashboard_url": _validate_langfuse_dashboard_url,
    # Automation
    "automation.fetch_interval_hours": _validate_positive_int,
    # Cloud LLM provider keys
    "llm.anthropic.api_key": _validate_nonempty_str,
    "llm.openai.api_key": _validate_nonempty_str,
    "llm.google.api_key": _validate_nonempty_str,
    # SMTP outbound mail
    "smtp.host": _validate_nonempty_str,
    "smtp.port": _validate_positive_int,
    "smtp.user": _validate_nonempty_str,
    "smtp.from": _validate_nonempty_str,
    "smtp.pass": _validate_nonempty_str,
}
