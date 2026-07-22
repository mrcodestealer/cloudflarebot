"""Remote deploy for the /deploy command: git pull + service restart.

Triggered only by an allowlisted admin in a 1:1 PM (authorization is enforced in
main.py). Everything here runs on a worker thread so the WebSocket handler never
blocks.

The restart is launched via ``systemd-run`` so it executes in its own transient
unit -- it therefore survives this process being killed by the restart itself
(a plain ``systemctl restart`` from inside the unit would race its own SIGTERM).
Just before restarting we drop a marker file; on the next startup
``notify_if_redeployed()`` reads it and posts a "back online" confirmation,
closing the loop so the admin knows the new build actually came up.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from typing import List

from config import config

log = logging.getLogger("deploy")

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
_STATE_DIR = (
    config.state_dir if os.path.isabs(config.state_dir)
    else os.path.join(REPO_DIR, config.state_dir)
)
_MARKER = os.path.join(_STATE_DIR, "redeploy_notify.json")


def _run(cmd: List[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=REPO_DIR, capture_output=True, text=True, timeout=timeout)


def _restart_cmd() -> List[str]:
    raw = config.deploy_restart_cmd.strip() or (
        f"systemd-run --no-block systemctl restart {config.deploy_service}"
    )
    return shlex.split(raw)


def _git_head() -> str:
    try:
        p = _run(["git", "rev-parse", "--short", "HEAD"], timeout=15)
        return p.stdout.strip() or "?"
    except Exception:
        return "?"


def _write_marker(chat_id: str, message_id: str, commit: str) -> None:
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        with open(_MARKER, "w", encoding="utf-8") as f:
            json.dump({"chat_id": chat_id, "message_id": message_id, "commit": commit}, f)
    except Exception:
        log.exception("could not write redeploy marker")


def _clear_marker() -> None:
    try:
        os.remove(_MARKER)
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("could not remove redeploy marker")


def run_deploy(lark_bot, chat_id: str, message_id: str) -> None:
    """git pull + restart, reporting progress to the PM. Blocking -- call on a thread."""
    lark_bot.add_reaction(message_id, config.lark_reaction_processing)  # 👌 working
    try:
        before = _git_head()
        try:
            p = _run(["git", "pull", "--ff-only"], timeout=120)
        except subprocess.TimeoutExpired:
            lark_bot.send_text(chat_id, "⛔ Deploy aborted: `git pull` timed out.", message_id)
            return
        except FileNotFoundError as exc:
            lark_bot.send_text(chat_id, f"⛔ Deploy aborted: git not found ({exc}).", message_id)
            return

        out = (p.stdout + p.stderr).strip() or "(no output)"
        if p.returncode != 0:
            lark_bot.send_text(
                chat_id,
                f"⛔ Deploy aborted — `git pull` failed (exit {p.returncode}):\n{out}",
                message_id,
            )
            return

        after = _git_head()
        head_note = (
            f"Already up to date at `{after}` — restarting anyway."
            if before == after
            else f"Updated `{before}` → `{after}`."
        )
        lark_bot.send_text(
            chat_id, f"✅ git pull:\n{out}\n\n{head_note}\n♻️ Restarting service…", message_id
        )

        # Drop the marker first, then hand the restart to systemd. Once the new
        # process starts it posts the "back online" confirmation.
        _write_marker(chat_id, message_id, after)
        try:
            subprocess.Popen(_restart_cmd(), cwd=REPO_DIR, start_new_session=True)
        except FileNotFoundError as exc:
            _clear_marker()
            lark_bot.send_text(
                chat_id,
                f"⛔ Pull succeeded but the restart command was not found ({exc}). "
                f"Restart the service manually.",
                message_id,
            )
            return
        # systemd will now stop this process; nothing after this is guaranteed to run.
    except Exception:
        log.exception("run_deploy failed")
        try:
            lark_bot.send_text(chat_id, "⛔ Deploy failed: internal error (see server logs).", message_id)
        except Exception:
            pass
    finally:
        lark_bot.add_reaction(message_id, config.lark_reaction_done)  # ✅ processed


def notify_if_redeployed(lark_bot) -> None:
    """On startup: if a /deploy just restarted us, post a 'back online' confirmation."""
    try:
        with open(_MARKER, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except Exception:
        log.exception("could not read redeploy marker")
        _clear_marker()
        return

    _clear_marker()
    chat_id = data.get("chat_id")
    if not chat_id:
        return
    commit = data.get("commit", "?")
    try:
        lark_bot.send_text(chat_id, f"✅ Back online — deployed `{commit}`.", data.get("message_id"))
    except Exception:
        log.exception("could not send back-online notice")
