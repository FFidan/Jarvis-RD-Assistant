#!/usr/bin/env python3
"""Dry-run or apply rotation for encrypted user_config rows.

Reads DATABASE_URL, OLD_JARVIS_CONFIG_KEY, and NEW_JARVIS_CONFIG_KEY from the
environment. By default this validates decryptability and reports counts only.
Use ``--apply`` to write re-encrypted ciphertexts in a single transaction.
"""

from __future__ import annotations

import argparse
import asyncio
import os

import asyncpg
from cryptography.fernet import Fernet


def _required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"{name} is required")
    return value


async def _rotate(*, apply: bool) -> None:
    database_url = _required_env("DATABASE_URL")
    old_key = _required_env("OLD_JARVIS_CONFIG_KEY").encode("ascii")
    new_key = _required_env("NEW_JARVIS_CONFIG_KEY").encode("ascii")
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
            updates: list[tuple[bytes, int]] = []
            for row in rows:
                encrypted_value = row["encrypted_value"]
                if isinstance(encrypted_value, memoryview):
                    encrypted_value = encrypted_value.tobytes()
                ciphertext = bytes(encrypted_value).decode("ascii")
                plaintext = old_fernet.decrypt(ciphertext.encode("ascii"))
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write rotated ciphertexts")
    args = parser.parse_args()
    asyncio.run(_rotate(apply=args.apply))


if __name__ == "__main__":
    main()
