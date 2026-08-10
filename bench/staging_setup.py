"""Ensure Condor's config.yml has a dedicated bench staging server + ACL.

Live bench resolves Hummingbot MCP through Condor's production wiring, which
looks up a *named* server entry. Operators should only need to enter the API URL
and credentials in Settings — this module owns the rest:

* a fixed internal server name (``bench_staging``)
* a fixed bench user / chat id (isolated from real Telegram/web chats)
* server entry upsert, ownership, and chat default

Call :func:`ensure_bench_server` after Settings saves staging fields, and from
``staging-check`` / ``register_bench_server.py`` so CLI runs heal the same way.
"""
from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from config import BENCH_CHAT_ID, BENCH_SERVER_NAME, BENCH_USER_ID, condor_path, staging_config

log = logging.getLogger(__name__)


@dataclass
class BenchServerSync:
    ok: bool
    detail: str
    server_name: str = BENCH_SERVER_NAME
    host: str | None = None
    port: int | None = None
    user_id: int = BENCH_USER_ID
    chat_id: int = BENCH_CHAT_ID
    actions: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "detail": self.detail,
            "server_name": self.server_name,
            "host": self.host,
            "port": self.port,
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "actions": list(self.actions or []),
        }


def ensure_bench_server(
    *,
    api_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    dry_run: bool = False,
) -> BenchServerSync:
    """Upsert the bench server entry and grant the bench identity access.

    Returns a result even on failure — callers decide whether to surface it.
    """
    staging = staging_config()
    api_url = (api_url if api_url is not None else str(staging["api_url"] or "")).rstrip("/")
    username = username if username is not None else str(staging["username"] or "")
    password = password if password is not None else str(staging["password"] or "")
    name = BENCH_SERVER_NAME
    user_id = BENCH_USER_ID
    chat_id = BENCH_CHAT_ID
    actions: list[str] = []

    if not api_url:
        return BenchServerSync(
            ok=False,
            detail="HUMMINGBOT_API_URL is not set — nothing to register",
            actions=actions,
        )

    parsed = urlparse(api_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        return BenchServerSync(
            ok=False,
            detail=f"Could not parse a host out of HUMMINGBOT_API_URL={api_url!r}",
            actions=actions,
        )

    if not username or not password:
        return BenchServerSync(
            ok=False,
            detail="HUMMINGBOT_USERNAME / HUMMINGBOT_PASSWORD are required",
            host=host,
            port=port,
            actions=actions,
        )

    repo = condor_path()
    if repo is None:
        return BenchServerSync(
            ok=False,
            detail="No condor checkout found. Set CONDOR_PATH=/path/to/condor.",
            host=host,
            port=port,
            actions=actions,
        )

    if dry_run:
        return BenchServerSync(
            ok=True,
            detail=f"dry run — would register {name} → {host}:{port} for user {user_id}",
            host=host,
            port=port,
            actions=["dry_run"],
        )

    try:
        cm = _config_manager(repo)
    except Exception as exc:
        return BenchServerSync(
            ok=False,
            detail=f"Could not load condor config.yml: {exc}",
            host=host,
            port=port,
            actions=actions,
        )

    try:
        _ensure_bench_user(cm, user_id, actions)
        _upsert_server(cm, name, host, port, username, password, user_id, actions)
        _ensure_ownership(cm, name, user_id, actions)
        if cm.set_chat_default_server(chat_id, name):
            actions.append(f"chat_default[{chat_id}]={name}")
        else:
            return BenchServerSync(
                ok=False,
                detail=f"Failed to set chat default for {chat_id} → {name}",
                host=host,
                port=port,
                actions=actions,
            )
    except Exception as exc:
        log.exception("ensure_bench_server failed")
        return BenchServerSync(
            ok=False,
            detail=f"Failed to sync bench server: {exc}",
            host=host,
            port=port,
            actions=actions,
        )

    resolved = cm.get_server(name) or {}
    return BenchServerSync(
        ok=True,
        detail=(
            f"Registered '{name}' → {resolved.get('host', host)}:"
            f"{resolved.get('port', port)} (user {user_id}, chat {chat_id})"
        ),
        host=str(resolved.get("host", host)),
        port=int(resolved.get("port", port)),
        actions=actions,
    )


def _config_manager(repo: Path) -> Any:
    """Load ConfigManager and bind it to ``repo/config.yml``.

    The checkout that owns ``config.yml`` (CONDOR_PATH) may be a thin test stub.
    Prefer importing ``config_manager`` from a full condor tree already on
    ``sys.path`` (or from ``repo`` when it has the module), then point the
    singleton at this config file.
    """
    path = (repo / "config.yml").resolve()
    roots: list[Path] = []
    if (repo / "config_manager.py").is_file():
        roots.append(repo)
    for entry in list(sys.path):
        candidate = Path(entry)
        if (candidate / "config_manager.py").is_file() and candidate.resolve() not in {
            r.resolve() for r in roots
        }:
            roots.append(candidate)
    if not roots:
        roots.append(repo)

    last_err: Exception | None = None
    ConfigManager = None
    for root in roots:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        try:
            from config_manager import ConfigManager as _CM  # noqa: PLC0415

            ConfigManager = _CM
            break
        except ModuleNotFoundError as exc:
            last_err = exc
            continue
    if ConfigManager is None:
        raise ModuleNotFoundError(
            f"Could not import config_manager for {repo}"
        ) from last_err

    existing = ConfigManager._instance
    if existing is not None and Path(existing.config_path).resolve() != path:
        ConfigManager._instance = None
    return ConfigManager.instance(str(path))


def _ensure_bench_user(cm: Any, user_id: int, actions: list[str]) -> None:
    from config_manager import UserRole  # noqa: PLC0415

    users = cm._data.setdefault("users", {})
    user = users.get(user_id)
    if user is None:
        users[user_id] = {
            "user_id": user_id,
            "username": "bench",
            "role": UserRole.USER.value,
            "created_at": time.time(),
            "notes": "Auto-created by condor-bench staging setup",
        }
        cm._save_config()
        actions.append(f"created_user[{user_id}]")
        return
    if user.get("role") in (UserRole.PENDING.value, UserRole.BLOCKED.value, None):
        user["role"] = UserRole.USER.value
        cm._save_config()
        actions.append(f"approved_user[{user_id}]")


def _upsert_server(
    cm: Any,
    name: str,
    host: str,
    port: int,
    username: str,
    password: str,
    owner_id: int,
    actions: list[str],
) -> None:
    existing = cm.get_server(name)
    if existing is None:
        ok = cm.add_server(
            name, host=host, port=port, username=username, password=password, owner_id=owner_id
        )
        if not ok:
            raise RuntimeError(f"config_manager refused to add server '{name}'")
        actions.append("added_server")
        return
    ok = cm.modify_server(
        name, host=host, port=port, username=username, password=password
    )
    if not ok:
        raise RuntimeError(f"config_manager refused to modify server '{name}'")
    actions.append("updated_server")


def _ensure_ownership(cm: Any, name: str, user_id: int, actions: list[str]) -> None:
    access = cm._data.setdefault("server_access", {})
    entry = access.get(name)
    if entry is None:
        access[name] = {
            "owner_id": user_id,
            "created_at": time.time(),
            "shared_with": {},
        }
        cm._save_config()
        actions.append("registered_owner")
        return

    if entry.get("owner_id") == user_id:
        return

    # Dedicated bench entry — take ownership so manage_servers status works
    # without depending on a human admin share grant.
    entry["owner_id"] = user_id
    entry.setdefault("shared_with", {})
    # Drop self from shared_with if present after becoming owner.
    entry["shared_with"].pop(user_id, None)
    cm._save_config()
    actions.append("took_ownership")