"""Read-only SessionDB queries used by the HTTP routes.

Mirrors the response shapes from ``hermes_cli/web_server.py`` (the
dashboard's FastAPI app) so a client written against ``/api/sessions/*``
can swap to ``/hermes/sessions/*`` with no schema diff.

Defensive imports: when Hermes core isn't reachable (e.g. the plugin was
loaded outside a Hermes venv during testing), we surface a structured
``unavailable`` error rather than crashing the HTTP server.

Connection lifecycle: each request opens its own ``SessionDB()``, runs
its query, and closes. SessionDB does an internal PASSIVE WAL checkpoint
on close, which keeps the WAL file from growing unbounded under bursty
read load. This matches what web_server.py does and is the documented
pattern for short-lived HTTP handlers.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Sources are free-form strings in Hermes (CLI uses "cli", gateway uses
# "gateway", messaging platforms use their own name). Any HTTP client of
# the backplane gets a single shared default — distinguishing browser-
# extension from "some script poked the endpoint" is the caller's job,
# not the wrapper's.
_DEFAULT_SOURCE = "backplane"

# Generous cap for an assistant turn: model output + reasoning + tool
# call args / results can comfortably exceed 64 KB. Stays well under the
# 50 MB attachment ceiling on the aiohttp app.
MAX_MESSAGE_BODY_BYTES = 4 * 1024 * 1024


def _unavailable_error(exc: Exception) -> str:
    return (
        f"Hermes session store unavailable: {type(exc).__name__}: {exc}. "
        "Make sure $HERMES_HOME/state.db exists and is accessible."
    )


def _open_db():
    """Open a fresh SessionDB. Returns ``(db, None)`` or ``(None, error_msg)``."""
    try:
        from hermes_state import SessionDB  # type: ignore
    except Exception as exc:
        logger.warning("hermes_state import failed: %s", exc)
        return None, _unavailable_error(exc)
    try:
        return SessionDB(), None
    except Exception as exc:
        logger.warning("SessionDB() failed: %s", exc)
        return None, _unavailable_error(exc)


# ---------------------------------------------------------------------------
# List sessions
# ---------------------------------------------------------------------------


# Activity threshold mirrored from web_server.py:786 — a session is
# considered "active" if it never ended and its last message landed
# within the last 5 minutes. Kept here as a named constant so changes
# upstream are easy to spot in a side-by-side diff.
_ACTIVE_WINDOW_S = 300


def list_sessions_response(
    limit: int = 20,
    offset: int = 0,
    source: Optional[str] = None,
    exclude_sources: Optional[list] = None,
) -> Dict[str, Any]:
    """List sessions, optionally filtered by ``source``.

    The browser extension uses ``exclude_sources=["cron"]`` to keep
    automated cron-job sessions out of the chat history sidebar — they
    aren't user conversations and clutter the list. The underlying
    ``SessionDB.list_sessions_rich`` natively understands both
    arguments, so this is a thin pass-through.

    Note: ``total`` is the count of sessions matching the same filter,
    not the global total. Reporting the unfiltered total would mislead
    paginating clients into requesting offsets that produce empty
    results.
    """
    db, err = _open_db()
    if db is None:
        return {"ok": False, "error": err}
    try:
        kwargs: Dict[str, Any] = {"limit": limit, "offset": offset}
        if source:
            kwargs["source"] = source
        if exclude_sources:
            kwargs["exclude_sources"] = list(exclude_sources)
        sessions = db.list_sessions_rich(**kwargs)
        total = db.session_count(source=source) if source else db.session_count()
        now = time.time()
        for s in sessions:
            s["is_active"] = (
                s.get("ended_at") is None
                and (now - s.get("last_active", s.get("started_at", 0)))
                < _ACTIVE_WINDOW_S
            )
        return {
            "ok": True,
            "sessions": sessions,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    except Exception as exc:
        logger.exception("list_sessions_rich failed")
        return {"ok": False, "error": _unavailable_error(exc)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Single session detail
# ---------------------------------------------------------------------------


def get_session_response(session_id: str) -> Dict[str, Any]:
    db, err = _open_db()
    if db is None:
        return {"ok": False, "error": err}
    try:
        # resolve_session_id accepts an exact id OR an unambiguous prefix —
        # useful for CLIs but also harmless from HTTP since the regex
        # ``[A-Za-z0-9_-]+`` matches one canonical session.
        sid: Optional[str] = db.resolve_session_id(session_id)
        session = db.get_session(sid) if sid else None
        if not session:
            return {"ok": False, "error": "session not found"}
        return {"ok": True, "session": session}
    except Exception as exc:
        logger.exception("get_session failed (id=%s)", session_id)
        return {"ok": False, "error": _unavailable_error(exc)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Session messages
# ---------------------------------------------------------------------------


def get_messages_response(session_id: str) -> Dict[str, Any]:
    db, err = _open_db()
    if db is None:
        return {"ok": False, "error": err}
    try:
        sid: Optional[str] = db.resolve_session_id(session_id)
        if not sid:
            return {"ok": False, "error": "session not found"}
        messages = db.get_messages(sid)
        return {"ok": True, "session_id": sid, "messages": messages}
    except Exception as exc:
        logger.exception("get_messages failed (id=%s)", session_id)
        return {"ok": False, "error": _unavailable_error(exc)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Create session
# ---------------------------------------------------------------------------


def create_session_response(body: Dict[str, Any]) -> Dict[str, Any]:
    """Create a session row. Mints a UUID when the caller omits ``id``.

    The contract for *body* is the create-side subset of SessionDB's session
    row: ``id`` (optional), ``source`` (defaults to "backplane"), ``model``,
    ``parent_session_id``, ``system_prompt``, ``user_id``, ``title``.

    The 'create' is two ops under the hood: ``create_session()`` to insert
    the row, then an optional ``set_session_title()`` if a title was passed.
    The title set runs in its own write transaction; on conflict the row is
    still created but the title is dropped — the response surfaces the
    conflict in ``title_error`` instead of failing the whole call. Callers
    can decide whether to retry with a different title; the session row is
    safe to use regardless.
    """
    db, err = _open_db()
    if db is None:
        return {"ok": False, "error": err}
    try:
        session_id = body.get("id")
        if session_id is not None and not isinstance(session_id, str):
            return {"ok": False, "error": "id must be a string when provided"}
        if not session_id:
            # uuid4().hex matches what hermes_state uses elsewhere for CLI
            # session ids (32-hex). Keeping the same shape avoids surprising
            # downstream readers that pattern-match on length.
            session_id = uuid.uuid4().hex
        source = body.get("source") if isinstance(body.get("source"), str) else _DEFAULT_SOURCE

        # Pass-through fields. Unknown keys are silently dropped: the
        # underlying ``_insert_session_row`` would reject them as TypeError,
        # and a 500-because-typo'd-field is bad ergonomics.
        kwargs: Dict[str, Any] = {}
        for key in ("model", "parent_session_id", "system_prompt", "user_id"):
            value = body.get(key)
            if value is not None:
                kwargs[key] = value
        model_config = body.get("model_config")
        if isinstance(model_config, dict):
            kwargs["model_config"] = model_config

        db.create_session(session_id, source, **kwargs)

        # Optional title — soft-fail on conflict (see docstring).
        title = body.get("title")
        title_error: Optional[str] = None
        if isinstance(title, str) and title.strip():
            try:
                db.set_session_title(session_id, title)
            except ValueError as exc:
                title_error = str(exc)

        session = db.get_session(session_id)
        result: Dict[str, Any] = {"ok": True, "session": session}
        if title_error:
            result["title_error"] = title_error
        return result
    except Exception as exc:
        logger.exception("create_session failed")
        return {"ok": False, "error": _unavailable_error(exc)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Auto-title (background)
# ---------------------------------------------------------------------------

# Mirrors the guard in ``agent.title_generator.maybe_auto_title``: only
# attempt naming during the first two user→assistant exchanges so we
# don't surprise users with a rename mid-conversation.
_AUTO_TITLE_USER_MSG_LIMIT = 2

# Polling parameters for the explicit ``/auto-title`` endpoint. The
# renderer fires this the instant it sees the chat-completion ``[DONE]``
# event, but the agent's own end-of-turn path (assistant message flush
# + ``maybe_auto_title`` background thread) runs concurrently — on the
# first turn of a fresh session the request can land BEFORE either has
# reached SessionDB. Polling lets the common case converge instead of
# returning ``no_first_exchange`` and leaving the session untitled until
# the NEXT turn happens to surface it via ``already_titled``.
_AUTO_TITLE_POLL_INTERVAL_S = 0.1
_AUTO_TITLE_POLL_MAX_ATTEMPTS = 10  # ~1s total wall time


def _content_to_text(content: Any) -> str:
    """Best-effort string projection of a SessionDB content value.

    Plain strings pass through; multimodal lists are JSON-serialised so
    ``generate_title`` (which slices to 500 chars) still has *something*
    to feed the prompt. Anything else gets coerced to ``str``.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # generate_title only uses the first 500 chars; a JSON dump is
        # adequate and avoids upstream having to walk the parts itself.
        try:
            import json
            return json.dumps(content, ensure_ascii=False)
        except Exception:
            return ""
    if content is None:
        return ""
    return str(content)


def _spawn_auto_title(
    session_id: str,
    user_message: str,
    assistant_response: str,
) -> None:
    """Fire-and-forget title generation against a fresh SessionDB.

    Why a fresh DB and not the caller's connection: the HTTP handler that
    triggered us closes its connection in the surrounding ``finally``,
    which could race the background thread mid-query. SessionDB is
    multi-process safe by design, so a per-thread instance is the
    cheapest correct path.

    Why we re-check the guards inside the thread even though we already
    checked outside: in the time between our outer check and the
    thread's first query, some other process (CLI, another HTTP
    handler) may have already set a title or appended more user
    messages. The guards are cheap; the LLM call is not.
    """

    def _run() -> None:
        try:
            from hermes_state import SessionDB  # type: ignore
            from agent.title_generator import generate_title  # type: ignore
        except Exception:
            logger.debug("auto-title imports unavailable", exc_info=True)
            return

        try:
            bg = SessionDB()
        except Exception:
            logger.debug("auto-title SessionDB() failed", exc_info=True)
            return

        try:
            # Re-check guards under the fresh connection.
            if bg.get_session_title(session_id):
                return
            history = bg.get_messages(session_id)
            user_msg_count = sum(
                1 for m in history if m.get("role") == "user"
            )
            if user_msg_count > _AUTO_TITLE_USER_MSG_LIMIT:
                return

            title = generate_title(user_message, assistant_response)
            if not title:
                return

            try:
                bg.set_session_title(session_id, title)
                logger.info(
                    "backplane auto-title set for %s: %s",
                    session_id, title,
                )
            except ValueError as exc:
                # Title collides with another session. Acceptable — the
                # session simply stays untitled rather than failing.
                logger.debug(
                    "auto-title set_session_title rejected (%s): %s",
                    session_id, exc,
                )
            except Exception:
                logger.debug(
                    "auto-title set_session_title failed", exc_info=True,
                )
        finally:
            try:
                bg.close()
            except Exception:
                pass

    threading.Thread(
        target=_run, daemon=True, name=f"auto-title-{session_id[:8]}",
    ).start()


def _maybe_trigger_auto_title(
    db: Any,
    session_id: str,
    appended_role: Optional[str],
    appended_content: Any,
    history_after_append: List[Dict[str, Any]],
) -> None:
    """Decide if the just-appended message warrants a title generation.

    Cheap synchronous pre-checks gate the (expensive) thread spawn. The
    thread re-checks under a fresh DB connection to absorb races.
    """
    if appended_role != "assistant":
        return

    assistant_text = _content_to_text(appended_content)
    if not assistant_text:
        return

    # Don't bother spawning if the session is already titled. The thread
    # would catch this too, but skipping here avoids the import + thread
    # cost on hot append loops.
    try:
        if db.get_session_title(session_id):
            return
    except Exception:
        # If even reading the title throws, downstream is in worse
        # shape — bail out of the optional path.
        return

    user_msg_count = sum(
        1 for m in history_after_append if m.get("role") == "user"
    )
    if user_msg_count > _AUTO_TITLE_USER_MSG_LIMIT:
        return

    # Find the latest user message before this assistant message —
    # generate_title pairs the two for its prompt. Walk history in
    # reverse, skipping the assistant we just appended.
    last_user_text = ""
    for m in reversed(history_after_append):
        if m.get("role") == "user":
            last_user_text = _content_to_text(m.get("content"))
            if last_user_text:
                break
    if not last_user_text:
        return

    _spawn_auto_title(session_id, last_user_text, assistant_text)


# ---------------------------------------------------------------------------
# Append message
# ---------------------------------------------------------------------------


# Fields ``append_message`` accepts as keyword arguments. Anything else in
# the request body is dropped — append_message signature is large and
# evolving, and forwarding unknown kwargs would surface as opaque
# TypeError 500s when Hermes core adds/renames a field.
_APPEND_FIELDS = (
    "role",
    "content",
    "tool_name",
    "tool_calls",
    "tool_call_id",
    "token_count",
    "finish_reason",
    "reasoning",
    "reasoning_content",
    "reasoning_details",
    "codex_reasoning_items",
    "codex_message_items",
    "platform_message_id",
)


def append_message_response(session_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Append one message row. Returns ``{ok, message_id, message}``.

    Idempotency is the caller's problem: this is a thin pass-through. If
    the client retries a request, two rows land. SessionDB doesn't expose
    a uniqueness key the wrapper could enforce here without inventing one
    (e.g. client-supplied ``platform_message_id``), which the extension
    doesn't currently produce.
    """
    if not isinstance(body.get("role"), str) or not body["role"].strip():
        return {"ok": False, "error": "role is required"}

    db, err = _open_db()
    if db is None:
        return {"ok": False, "error": err}
    try:
        sid: Optional[str] = db.resolve_session_id(session_id)
        if not sid:
            return {"ok": False, "error": "session not found"}

        kwargs: Dict[str, Any] = {}
        for key in _APPEND_FIELDS:
            value = body.get(key)
            if value is not None:
                kwargs[key] = value

        message_id = db.append_message(sid, **kwargs)

        # Round-trip the freshly written row so clients have its rowid and
        # any DB-side timestamps without a second query. get_messages() is
        # O(messages), so for hot-loop appends this is a real cost on big
        # sessions — acceptable for the v0; if it bites, swap for a
        # dedicated single-row fetch.
        history = db.get_messages(sid)
        msg: Optional[Dict[str, Any]] = None
        for row in history:
            if row.get("id") == message_id:
                msg = row
                break

        # Auto-title runs off the same history snapshot we already fetched.
        # The trigger is fire-and-forget on a daemon thread, so the HTTP
        # response is not blocked on the LLM call.
        _maybe_trigger_auto_title(
            db,
            sid,
            kwargs.get("role"),
            kwargs.get("content"),
            history,
        )

        return {"ok": True, "session_id": sid, "message_id": message_id, "message": msg}
    except Exception as exc:
        logger.exception("append_message failed (sid=%s)", session_id)
        return {"ok": False, "error": _unavailable_error(exc)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Trigger auto-title (sync; LLM-generated via agent.title_generator)
# ---------------------------------------------------------------------------


def trigger_auto_title_response(
    session_id: str,
    body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Synchronously generate a session title from the first user/assistant
    exchange via ``agent.title_generator.generate_title`` and persist it.

    Returns ``{ok: True, title?: str, skipped?: bool, reason?: str}``. The
    caller is the chat client right after a stream finishes — running the
    LLM call synchronously keeps the wire shape simple (no polling, no
    refresh signal) at the cost of an extra second on the response. The
    hermes-x frontend treats this as a fire-and-forget after the assistant
    bubble lands, so the visible latency is post-stream.
    """
    del body  # No options today — accepted for forward compatibility.

    try:
        from agent.title_generator import generate_title  # type: ignore
    except Exception as exc:
        logger.debug("title_generator import failed", exc_info=True)
        return {"ok": False, "error": f"title_generator unavailable: {exc}"}

    db, err = _open_db()
    if db is None:
        return {"ok": False, "error": err}
    try:
        sid: Optional[str] = db.resolve_session_id(session_id)
        if not sid:
            return {"ok": False, "error": "session not found"}

        # Race-tolerant snapshot: re-read until either a title shows up
        # (background thread won) or both first-exchange messages are
        # present (we can generate ourselves). See poll-constants comment
        # above for why this exists.
        history: List[Dict[str, Any]] = []
        user_text = ""
        assistant_text = ""
        for _ in range(_AUTO_TITLE_POLL_MAX_ATTEMPTS):
            existing = db.get_session_title(sid)
            if existing:
                return {"ok": True, "skipped": True, "reason": "already_titled", "title": existing}

            history = db.get_messages(sid)
            # First user msg and the first assistant msg that follows it.
            user_text = ""
            assistant_text = ""
            seen_user = False
            for m in history:
                role = m.get("role")
                if role == "user" and not seen_user:
                    user_text = _content_to_text(m.get("content"))
                    seen_user = True
                elif role == "assistant" and seen_user:
                    assistant_text = _content_to_text(m.get("content"))
                    if assistant_text:
                        break
            if user_text and assistant_text:
                break

            time.sleep(_AUTO_TITLE_POLL_INTERVAL_S)

        if not user_text or not assistant_text:
            return {"ok": True, "skipped": True, "reason": "no_first_exchange"}

        user_msg_count = sum(1 for m in history if m.get("role") == "user")
        if user_msg_count > _AUTO_TITLE_USER_MSG_LIMIT:
            return {"ok": True, "skipped": True, "reason": "too_many_messages"}

        title = generate_title(user_text, assistant_text)
        if not title:
            return {"ok": True, "skipped": True, "reason": "generation_failed"}

        try:
            db.set_session_title(sid, title)
        except ValueError as exc:
            # Hermes enforces title uniqueness; collisions are common in
            # short demos. Report the generated title so the caller can
            # display it locally even though we couldn't persist.
            return {
                "ok": True,
                "skipped": True,
                "reason": "title_conflict",
                "title": title,
                "detail": str(exc),
            }

        return {"ok": True, "title": title}
    except Exception as exc:
        logger.exception("trigger_auto_title failed (sid=%s)", session_id)
        return {"ok": False, "error": _unavailable_error(exc)}
    finally:
        try:
            db.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Update session (title only for now)
# ---------------------------------------------------------------------------


def update_session_response(session_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Mutate session metadata. Today the only patchable field is ``title``.

    Routing maps the three failure cases distinctly:
    - ``title_conflict`` — another session owns that title → 409
    - ``invalid_title`` — sanitize/length rules rejected the value → 400
    - ``error: 'session not found'`` — 404

    Empty/whitespace title clears the title (Hermes-native semantics from
    ``sanitize_title``). The wrapper preserves that — no second-guessing.
    """
    db, err = _open_db()
    if db is None:
        return {"ok": False, "error": err}
    try:
        sid: Optional[str] = db.resolve_session_id(session_id)
        if not sid:
            return {"ok": False, "error": "session not found"}

        if "title" in body:
            title = body["title"]
            if title is not None and not isinstance(title, str):
                return {"ok": False, "error": "title must be a string or null"}
            try:
                db.set_session_title(sid, title or "")
            except ValueError as exc:
                msg = str(exc)
                kind = "title_conflict" if "already in use" in msg else "invalid_title"
                return {"ok": False, "error": msg, "kind": kind}

        session = db.get_session(sid)
        return {"ok": True, "session": session}
    except Exception as exc:
        logger.exception("update_session failed (id=%s)", session_id)
        return {"ok": False, "error": _unavailable_error(exc)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Delete session
# ---------------------------------------------------------------------------


def delete_session_response(session_id: str) -> Dict[str, Any]:
    db, err = _open_db()
    if db is None:
        return {"ok": False, "error": err}
    try:
        # delete_session takes a raw id (no prefix resolution): we resolve
        # first so the HTTP layer accepts the same forms as the read paths.
        sid: Optional[str] = db.resolve_session_id(session_id)
        if not sid:
            return {"ok": False, "error": "session not found"}
        if not db.delete_session(sid):
            # Defense against TOCTOU between resolve and delete — unlikely
            # but possible if another process pruned the row in between.
            return {"ok": False, "error": "session not found"}
        return {"ok": True}
    except Exception as exc:
        logger.exception("delete_session failed (id=%s)", session_id)
        return {"ok": False, "error": _unavailable_error(exc)}
    finally:
        db.close()
