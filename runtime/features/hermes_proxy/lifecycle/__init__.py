"""Backplane status + lifecycle actions (mirror of upstream Status page).

Three concerns wrapped together because they're how the Status page in
upstream's dashboard works as a unit:

- ``GET  /hermes/status``                        — overall snapshot (version,
   paths, gateway liveness, active session count)
- ``POST /hermes/gateway/restart``               — kick off ``hermes gateway
   restart`` as a detached subprocess
- ``POST /hermes/update``                        — kick off ``hermes update``
   (self-upgrade) as a detached subprocess
- ``GET  /hermes/actions/{name}/status``         — poll the long-running
   subprocess + tail its log file

Pattern lifted from upstream ``hermes_cli/web_server.py``:
``subprocess.Popen`` with ``start_new_session=True`` + a whitelist
mapping action name → log file → live Popen handle. The status endpoint
calls ``proc.poll()`` for liveness/exit_code and returns the last N
lines of the log so the UI can stream progress without WebSocket.
"""

from __future__ import annotations

from .routes import register

__all__ = ["register"]
