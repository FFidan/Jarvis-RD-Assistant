#!/usr/bin/env python3
"""Dry-run or apply rotation for encrypted user_config rows.

Reads the database connection and old/new Fernet keys from direct environment
variables or mounted secret files. By default this validates decryptability and
reports counts only. Use ``--apply`` to write re-encrypted ciphertexts in a
single transaction.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from urllib.parse import quote

import asyncpg
from cryptography.fernet import Fernet, InvalidToken


class ScriptError(RuntimeError):
    """Script-level error; caught by the __main__ block."""


def _required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ScriptError(f"{name} is required")
    return value


def _required_secret(name: str) -> str:
    """Read a required value from ``NAME`` or the file named by ``NAME_FILE``."""
    value = os.environ.get(name, "")
    if value:
        return value

    file_name = os.environ.get(f"{name}_FILE", "")
    if not file_name:
        raise ScriptError(f"{name} or {name}_FILE is required")
    try:
        value = Path(file_name).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ScriptError(f"cannot read {name}_FILE: {exc}") from exc
    if not value:
        raise ScriptError(f"{name}_FILE is empty")
    return value


def _database_url() -> str:
    """Resolve a direct URL or build one from a mounted Postgres password."""
    direct = os.environ.get("DATABASE_URL", "")
    if direct:
        return direct

    password_file = os.environ.get("POSTGRES_PASSWORD_FILE", "")
    if not password_file:
        raise ScriptError("DATABASE_URL or POSTGRES_PASSWORD_FILE is required")
    try:
        password = Path(password_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ScriptError(f"cannot read POSTGRES_PASSWORD_FILE: {exc}") from exc
    if not password:
        raise ScriptError("POSTGRES_PASSWORD_FILE is empty")

    user = quote(os.environ.get("POSTGRES_USER", "jarvis"), safe="")
    database = quote(os.environ.get("POSTGRES_DB", "jarvis"), safe="")
    host = os.environ.get("POSTGRES_HOST", "postgres")
    port = os.environ.get("POSTGRES_PORT", "5432")
    return f"postgresql://{user}:{quote(password, safe='')}@{host}:{port}/{database}"


def _classify_key_state(
    ciphertexts: list[bytes], old_fernet: Fernet, new_fernet: Fernet
) -> tuple[str, int]:
    """Classify all encrypted rows against the old and staged keys."""
    if not ciphertexts:
        return "empty", 0

    old_valid = True
    new_valid = True
    for ciphertext in ciphertexts:
        try:
            old_fernet.decrypt(ciphertext)
        except InvalidToken:
            old_valid = False
        try:
            new_fernet.decrypt(ciphertext)
        except InvalidToken:
            new_valid = False

    if old_valid and not new_valid:
        return "old", len(ciphertexts)
    if new_valid and not old_valid:
        return "new", len(ciphertexts)
    return "ambiguous", len(ciphertexts)


async def _rotate(*, apply: bool, probe_state: bool = False) -> None:
    database_url = _database_url()
    old_key = _required_secret("OLD_JARVIS_CONFIG_KEY").encode("ascii")
    new_key = _required_secret("NEW_JARVIS_CONFIG_KEY").encode("ascii")
    old_fernet = Fernet(old_key)
    new_fernet = Fernet(new_key)

    conn = await asyncpg.connect(database_url)
    try:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                SELECT id, key, encrypted_value
                FROM user_config
                WHERE encrypted_value IS NOT NULL
                ORDER BY key, id
                """
            )
            ciphertexts: list[bytes] = []
            for row in rows:
                encrypted_value = row["encrypted_value"]
                if isinstance(encrypted_value, memoryview):
                    encrypted_value = encrypted_value.tobytes()
                ciphertexts.append(bytes(encrypted_value))

            if probe_state:
                state, count = _classify_key_state(ciphertexts, old_fernet, new_fernet)
                print(f"JARVIS_ROTATION_STATE={state} ROWS={count}")
                return

            updates: list[tuple[bytes, int]] = []
            for row, ciphertext in zip(rows, ciphertexts, strict=True):
                plaintext = old_fernet.decrypt(ciphertext)
                updates.append((new_fernet.encrypt(plaintext), row["id"]))

            if apply:
                for ciphertext, row_id in updates:
                    await conn.execute(
                        """
                        UPDATE user_config
                        SET encrypted_value = $1, updated_at = NOW()
                        WHERE id = $2
                        """,
                        ciphertext,
                        row_id,
                    )

            print(
                f"{'rotated' if apply else 'validated'} {len(updates)} encrypted user_config rows"
            )
    finally:
        await conn.close()


def main() -> None:
    """Parse ``--apply`` flag and run the config-key rotation.

    Reads ``DATABASE_URL`` or ``POSTGRES_PASSWORD_FILE`` plus the old/new key
    variables (each of which also supports the ``_FILE`` convention).

    Raises
    ------
    ScriptError
        If any required environment variable is missing.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write rotated ciphertexts")
    mode.add_argument(
        "--probe-state",
        action="store_true",
        help="report whether all rows use the old key, new key, neither, or no key",
    )
    args = parser.parse_args()
    asyncio.run(_rotate(apply=args.apply, probe_state=args.probe_state))


if __name__ == "__main__":
    try:
        main()
    except ScriptError as exc:
        import sys as _sys

        print(f"ERROR: {exc}", file=_sys.stderr)
        _sys.exit(1)
