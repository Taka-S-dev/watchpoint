"""Tokens and API keys, kept out of the targets file.

They live in the OS credential store (Windows Credential Manager) under the
service name "watchpoint". An environment variable WATCHPOINT_SECRET_<NAME>
overrides it, which is handy for a scheduled task or a test.
"""

from __future__ import annotations

import os
import re

SERVICE = "watchpoint"


class MissingSecret(Exception):
    pass


def _env_name(name: str) -> str:
    return "WATCHPOINT_SECRET_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def clean_secret(value: str) -> str:
    """Strip what copy-paste adds around a key; refuse control characters (never quoting the value)."""
    value = value.strip()
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError("値に制御文字（改行・タブなど）が入っています。貼り付け直してください")
    return value


def get_secret(name: str) -> str:
    env = os.environ.get(_env_name(name))
    if env:
        return clean_secret(env)
    try:
        import keyring

        value = keyring.get_password(SERVICE, name)
    except Exception as e:  # noqa: BLE001 - no usable backend
        raise MissingSecret(f"資格情報を読めませんでした（{type(e).__name__}）: {name}") from e
    if not value:
        raise MissingSecret(
            f"資格情報「{name}」がありません。`watchpoint secret set {name}` で登録してください"
            f"（または環境変数 {_env_name(name)}）"
        )
    try:
        return clean_secret(value)
    except ValueError as e:
        raise MissingSecret(f"資格情報「{name}」の{e}（watchpoint secret set {name}）") from None


def has_secret(name: str) -> bool:
    try:
        get_secret(name)
        return True
    except MissingSecret:
        return False


def set_secret(name: str, value: str) -> None:
    import keyring

    keyring.set_password(SERVICE, name, clean_secret(value))


def delete_secret(name: str) -> bool:
    import keyring
    from keyring.errors import PasswordDeleteError

    try:
        keyring.delete_password(SERVICE, name)
        return True
    except PasswordDeleteError:
        return False
