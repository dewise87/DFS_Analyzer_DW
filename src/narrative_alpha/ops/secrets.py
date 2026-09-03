"""Ephemeral credential lookup for operator-lane provider construction."""

from __future__ import annotations

import os
import subprocess

from narrative_alpha.ops.config import OpsConfig

KEYCHAIN_LOOKUP_TIMEOUT_SECONDS = 15.0


def anthropic_api_key(config: OpsConfig) -> str | None:
    """Return the active Anthropic credential without persisting or logging it.

    A terminal or launchd environment takes precedence.  An interactive dashboard and a
    plain ``na-ops batch`` terminal otherwise read the same login-Keychain item as the
    scheduled wrapper.  A missing item intentionally has no diagnostic here: callers
    state the safe operator remedy without risking Keychain output in their history.
    """

    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value
    try:
        completed = subprocess.run(
            (
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                config.keychain_service,
                "-w",
            ),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            # A locked Keychain with a GUI session attached can raise a dialog and block
            # this call for as long as nobody answers it. A lane that is "running" for
            # ever is the silent failure this tool exists to prevent, so the lookup gives
            # up and the caller states the remedy instead.
            timeout=KEYCHAIN_LOOKUP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    key = completed.stdout.strip()
    return key or None
