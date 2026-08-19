"""Shared value validators for Platform-owned configuration keys."""

import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger

from jarvis_common.llm_provider_registry import (
    PROVIDER_API_KEY_CONFIG_KEYS,
    PROVIDER_BASE_URL_CONFIG_KEYS,
    validate_custom_openai_base_url,
)

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
    "_validate_optional_email",
    "_validate_optional_header_str",
    "_validate_library_type",
    "_validate_group_id",
    "_validate_zotero_cron",
    "_validate_zotero_allowed_private_hosts",
    "_validate_langfuse_dashboard_url",
    "validate_custom_openai_base_url",
    "_validate_fsrs_retention",
    "_validate_fsrs_learning_steps",
    "_validate_timezone",
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
# Shared cron validation: type/parse the expression, then enforce the
# schedule's own minimum-fire-interval floor.
# ---------------------------------------------------------------------------


def _validate_cron_base(v: Any, name: str) -> CronTrigger:
    """Shared cron validation: type-check and parse; return the trigger on success."""
    if not isinstance(v, str):
        raise ValueError(f"{name} must be a string")
    try:
        return CronTrigger.from_crontab(v)
    except Exception as exc:
        raise ValueError(f"invalid cron expression: {exc}") from exc


#: A full day plus an hour: long enough to see every gap a day-repeating
#: schedule produces, whatever time of day the check starts from.
_SCHEDULE_SCAN_WINDOW = timedelta(hours=25)

#: Hard stop on the walk, so no expression can spin here. Clearing the tightest
#: floor in use (fifteen minutes) allows at most a hundred fire times in the
#: window, and an expression that dips below its floor stops at the first pair.
_SCHEDULE_SCAN_MAX_FIRES = 200


def _now() -> datetime:
    """Current local time — a seam so schedule-floor checks can be pinned in tests."""
    return datetime.now()


def _validate_cron_min_interval(trigger: CronTrigger, *, minimum: timedelta, message: str) -> None:
    """Reject *trigger* if any two consecutive fire times are closer than *minimum*.

    Measuring only the next two fire times would let the wall clock decide:
    ``0,50,51 * * * *`` shows a one-minute gap when saved at ten past the hour
    and a fifty-minute one when saved at five to, so the same expression would
    be rejected or accepted by the minute it was saved on. Walking the schedule
    across the whole window finds the smallest gap it really produces.

    A schedule that fires less often than the window yields fewer than two fire
    times and is accepted — a weekly sync is above any floor enforced here.
    """
    previous = trigger.get_next_fire_time(None, _now())
    if previous is None:
        return
    deadline = previous + _SCHEDULE_SCAN_WINDOW
    for _ in range(_SCHEDULE_SCAN_MAX_FIRES):
        current = trigger.get_next_fire_time(previous, previous)
        if current is None or current > deadline:
            return
        if current - previous < minimum:
            raise ValueError(message)
        previous = current


def _validate_cron(v: Any) -> None:
    trigger = _validate_cron_base(v, "pulse.cron")
    # Reject sub-hourly schedules — pulse runs are expensive; once per hour is the minimum.
    _validate_cron_min_interval(
        trigger,
        minimum=timedelta(hours=1),
        message="Pulse cron must fire no more than once per hour",
    )


def _validate_zotero_cron(v: Any) -> None:
    if not isinstance(v, str):
        raise ValueError("zotero.poll_cron must be a string")
    trigger = _validate_cron_base(v, "zotero.poll_cron")
    # Reject schedules faster than every fifteen minutes — Zotero is a
    # third-party API and must not be hammered by an over-eager sync.
    _validate_cron_min_interval(
        trigger,
        minimum=timedelta(minutes=15),
        message="Zotero sync must run no more than once every 15 minutes",
    )


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


def _validate_optional_email(v: Any) -> None:
    """Optional email — empty string clears it; otherwise validate shape.

    Mirrors the dedicated /api/setup/smtp regex and rejects control characters so
    the generic /api/config write path is as strict as the wizard/settings path.
    """
    if v in (None, ""):
        return
    if not isinstance(v, str):
        raise ValueError("smtp.reply_to must be a string")
    s = v.strip()
    if not s.isprintable() or not re.match(r"^\S+@\S+\.\S+$", s):
        raise ValueError("smtp.reply_to must be a valid email address")


def _validate_optional_header_str(v: Any) -> None:
    """Optional single-line header value — empty clears it; reject control chars."""
    if v in (None, ""):
        return
    if not isinstance(v, str):
        raise ValueError("value must be a string")
    s = v.strip()
    if len(s) > 255:
        raise ValueError("value must be at most 255 characters")
    if any(c in s for c in ("\r", "\n", "\x00")) or not s.isprintable():
        raise ValueError("value must not contain control characters")


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


_MAX_ALLOWED_PRIVATE_HOSTS = 20
_BARE_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")


def _validate_zotero_allowed_private_hosts(v: Any) -> None:
    """Validate zotero.allowed_private_hosts — bare hostnames, at most twenty.

    Bare hostnames only: the value is compared against a parsed URL's host, so a
    scheme, path or port in an entry could never match and would read as an
    allowlist entry that silently does nothing.
    """
    if not isinstance(v, list):
        raise ValueError("zotero.allowed_private_hosts must be a list of hostnames")
    if len(v) > _MAX_ALLOWED_PRIVATE_HOSTS:
        raise ValueError(
            f"zotero.allowed_private_hosts accepts at most {_MAX_ALLOWED_PRIVATE_HOSTS} hostnames"
        )
    for entry in v:
        if not isinstance(entry, str) or not _BARE_HOSTNAME_RE.fullmatch(entry):
            raise ValueError(
                "zotero.allowed_private_hosts entries must be bare hostnames "
                "without a scheme, port or path"
            )


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
    """Validate fsrs.learning_steps — list of 1–10 positive integers (minutes)."""
    if not isinstance(v, list):
        raise ValueError("fsrs.learning_steps must be a list")
    if len(v) < 1:
        raise ValueError("fsrs.learning_steps must have at least 1 element")
    if len(v) > 10:
        raise ValueError("fsrs.learning_steps must have at most 10 elements")
    for i, step in enumerate(v):
        if isinstance(step, bool) or not isinstance(step, int) or step <= 0:
            raise ValueError(f"fsrs.learning_steps[{i}] must be a positive integer (minutes)")


def _validate_timezone(v: Any) -> None:
    """Validate user.timezone — must be a known IANA timezone string.

    Only fires on write; already-stored invalid values survive at read time
    (the scheduler falls back to UTC for unrecognised zones).
    """
    if not isinstance(v, str):
        raise ValueError("user.timezone must be a string")
    try:
        ZoneInfo(v)
    except (ZoneInfoNotFoundError, KeyError):
        raise ValueError(f"unknown timezone: {v!r}")


_APPEARANCE_KEYS = frozenset({"theme", "accent", "type", "density"})

_APPEARANCE_VALUES = {
    "theme": frozenset({"light", "dark", "system"}),
    "accent": frozenset({"ink-blue", "forest", "burgundy", "slate", "plum"}),
    "type": frozenset({"serif-calm", "sans-modern", "editorial", "legacy"}),
    "density": frozenset({"comfortable", "default", "compact"}),
}
#: Accepted range for each ``ui.timer`` field. This is the one definition of the
#: focus-timer contract: the web app writes these preferences, the validator
#: below rejects anything outside them, and Platform serves them to the Telegram
#: bot, so all three agree by construction.
TIMER_RANGES: dict[str, tuple[int, int]] = {
    "workMinutes": (15, 60),
    "shortBreakMinutes": (3, 15),
    "longBreakMinutes": (10, 30),
    "targetCycles": (2, 8),
}

#: Values applied when a reader has never saved a timer preference. They mirror
#: the web app's initial store state, so a session started from any surface is
#: the same length.
TIMER_DEFAULTS: dict[str, int] = {
    "workMinutes": 25,
    "shortBreakMinutes": 5,
    "longBreakMinutes": 15,
    "targetCycles": 4,
}

_TIMER_RANGES = TIMER_RANGES


def _validate_ui_appearance(value: Any) -> None:
    """Validate the complete account-backed appearance preference."""
    if not isinstance(value, dict) or set(value) != _APPEARANCE_KEYS:
        raise ValueError("ui.appearance must contain theme, accent, type, and density")
    for field, allowed in _APPEARANCE_VALUES.items():
        if not isinstance(value[field], str) or value[field] not in allowed:
            raise ValueError(f"ui.appearance.{field} has an unsupported value")


def _validate_ui_timer(value: Any) -> None:
    """Validate the complete account-backed timer preference."""
    if not isinstance(value, dict) or set(value) != set(_TIMER_RANGES):
        raise ValueError("ui.timer must contain all four timer settings")
    for field, (minimum, maximum) in _TIMER_RANGES.items():
        field_value = value[field]
        if (
            isinstance(field_value, bool)
            or not isinstance(field_value, int)
            or not minimum <= field_value <= maximum
        ):
            raise ValueError(f"ui.timer.{field} must be an integer from {minimum} to {maximum}")


def _validate_ui_nav_mode(value: Any) -> None:
    """Validate the account-backed navigation density preference."""
    if not isinstance(value, str) or value not in {"simple", "full"}:
        raise ValueError("ui.nav_mode must be 'simple' or 'full'")


_CONFIG_VALIDATORS: dict[str, Callable[[Any], None]] = {
    "ui.appearance": _validate_ui_appearance,
    "ui.timer": _validate_ui_timer,
    "ui.nav_mode": _validate_ui_nav_mode,
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
    "pulse.classifier_opt_in": _validate_bool,
    "recommendation.enabled": _validate_bool,
    "setup.completed": _validate_bool,
    "onboarding.dismissed": _validate_bool,
    "user.timezone": _validate_timezone,
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
    "zotero.allowed_private_hosts": _validate_zotero_allowed_private_hosts,
    "zotero.auto_push_on_star": _validate_bool,
    # Observability
    "observability.langfuse_dashboard_url": _validate_langfuse_dashboard_url,
    # Automation
    "automation.fetch_interval_hours": _validate_positive_int,
    "automation.auto_summarize_discovered": _validate_bool,
    # Cloud LLM provider keys
    **{key: _validate_nonempty_str for key in PROVIDER_API_KEY_CONFIG_KEYS},
    **{key: validate_custom_openai_base_url for key in PROVIDER_BASE_URL_CONFIG_KEYS},
    # SMTP outbound mail
    "smtp.host": _validate_nonempty_str,
    "smtp.port": _validate_positive_int,
    "smtp.user": _validate_nonempty_str,
    "smtp.from": _validate_nonempty_str,
    "smtp.pass": _validate_nonempty_str,
    # Optional sender identity (admin-set): empty string clears.
    "smtp.reply_to": _validate_optional_email,
    "smtp.from_name": _validate_optional_header_str,
}
