"""Credential storage: OS keyring when available, JSON file otherwise."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .config import KEYRING_SERVICE, log, resolve_creds_path

try:
    import keyring
    KEYRING_AVAILABLE = True
except ImportError:
    keyring = None
    KEYRING_AVAILABLE = False


@dataclass(frozen=True)
class Credentials:
    username: str
    api_key: str

    def is_complete(self) -> bool:
        return bool(self.username and self.api_key)


def storage_description() -> str:
    return ("system keyring" if KEYRING_AVAILABLE
            else f"local file ({resolve_creds_path()})")


def load() -> Credentials | None:
    """Read stored credentials, checking both backends.

    Item #15: the old code branched on KEYRING_AVAILABLE and consulted exactly
    one store. Installing keyring therefore "lost" credentials that were sitting
    in the fallback file. Now the keyring is preferred but the file is still
    consulted, and anything found there is migrated up.
    """
    if KEYRING_AVAILABLE:
        creds = _load_keyring()
        if creds:
            return creds
        creds = _load_file()
        if creds:
            log.info("Migrating credentials from %s into the system keyring",
                     resolve_creds_path())
            save(creds)
            _delete_file()
            return creds
        return None

    return _load_file()


def save(creds: Credentials) -> None:
    if KEYRING_AVAILABLE:
        try:
            keyring.set_password(KEYRING_SERVICE, "username", creds.username)
            keyring.set_password(KEYRING_SERVICE, "api_key", creds.api_key)
            return
        except Exception as exc:
            log.warning("Keyring write failed (%s); falling back to %s",
                        exc, resolve_creds_path())
    _save_file(creds)


def clear() -> None:
    if KEYRING_AVAILABLE:
        for field in ("username", "api_key"):
            try:
                keyring.delete_password(KEYRING_SERVICE, field)
            except Exception:
                # Absent entries raise; that is not a failure to delete.
                pass
    _delete_file()


# --- backends ---------------------------------------------------------------

def _load_keyring() -> Credentials | None:
    try:
        username = keyring.get_password(KEYRING_SERVICE, "username")
        api_key = keyring.get_password(KEYRING_SERVICE, "api_key")
    except Exception as exc:
        log.warning("Could not read the system keyring: %s", exc)
        return None
    if username and api_key:
        return Credentials(username, api_key)
    return None


def _load_file() -> Credentials | None:
    path = resolve_creds_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # Item #7: was a bare `except:`, which also swallowed KeyboardInterrupt.
        log.warning("Could not read %s: %s", path, exc)
        return None
    username, api_key = data.get("username"), data.get("api_key")
    return Credentials(username, api_key) if username and api_key else None


def _save_file(creds: Credentials) -> None:
    path = resolve_creds_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(
        {"username": creds.username, "api_key": creds.api_key}), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass          # no-op on Windows


def _delete_file() -> None:
    try:
        resolve_creds_path().unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Could not remove credentials file: %s", exc)
