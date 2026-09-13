"""Opt-in HTTP tracing for Blink API requests.

Enable with BLINK_HTTP_DEBUG=true. The tracer logs requests, responses,
redirects, timing, headers, and bounded textual bodies while redacting
credentials, tokens, cookies, 2FA values, and other authentication secrets.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiohttp import TraceConfig


LOGGER = logging.getLogger("blink.http")
DEFAULT_BODY_LIMIT = 8192

_SECRET_FRAGMENTS = (
    "authorization",
    "cookie",
    "password",
    "passwd",
    "secret",
    "token",
    "csrf",
    "2fa",
    "otp",
    "pin",
    "code_verifier",
    "code_challenge",
    "hardware_id",
    "device_id",
    "api_key",
    "apikey",
)
_IDENTITY_FRAGMENTS = ("username", "email")
_BINARY_CONTENT_PREFIXES = ("image/", "video/", "audio/", "application/octet-stream")


def blink_http_debug_enabled() -> bool:
    """Return whether verbose Blink HTTP tracing is enabled."""
    return os.getenv("BLINK_HTTP_DEBUG", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _body_limit() -> int:
    value = os.getenv("BLINK_HTTP_DEBUG_BODY_LIMIT", str(DEFAULT_BODY_LIMIT))
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return DEFAULT_BODY_LIMIT


def _is_sensitive_name(name: str) -> bool:
    lowered = name.lower().replace("-", "_")
    return any(fragment in lowered for fragment in _SECRET_FRAGMENTS)


def _is_identity_name(name: str) -> bool:
    lowered = name.lower().replace("-", "_")
    return any(fragment in lowered for fragment in _IDENTITY_FRAGMENTS)


def _redact_value(name: str, value: Any) -> Any:
    if _is_sensitive_name(name) or _is_identity_name(name):
        return "[REDACTED]"
    return value


def _redact_mapping(mapping: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        items = mapping.items()
    except AttributeError:
        return result

    for key, value in items:
        key_str = str(key)
        result[key_str] = _redact_value(key_str, value)
    return result


def _redact_url(url: Any) -> str:
    """Redact sensitive query-string values from a URL."""
    raw = str(url)
    try:
        parts = urlsplit(raw)
        query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            query.append((key, str(_redact_value(key, value))))
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )
    except Exception:
        return raw


def _redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if _is_sensitive_name(str(key)) or _is_identity_name(str(key))
                else _redact_json(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    return value


def _redact_text(text: str, content_type: str = "") -> str:
    """Redact common JSON/form authentication fields from textual payloads."""
    stripped = text.strip()

    if "json" in content_type.lower() or stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(text)
            return json.dumps(_redact_json(parsed), ensure_ascii=False, separators=(",", ":"))
        except (json.JSONDecodeError, TypeError):
            pass

    if "x-www-form-urlencoded" in content_type.lower() or ("=" in text and "&" in text):
        try:
            pairs = parse_qsl(text, keep_blank_values=True)
            if pairs:
                return urlencode(
                    [(key, str(_redact_value(key, value))) for key, value in pairs]
                )
        except Exception:
            pass

    # Best-effort fallback for JSON-like or key=value secrets in plain text.
    redacted = text
    sensitive_pattern = "|".join(re.escape(item) for item in _SECRET_FRAGMENTS + _IDENTITY_FRAGMENTS)
    redacted = re.sub(
        rf'(?i)(["\']?(?:{sensitive_pattern})["\']?\s*[:=]\s*)["\']?[^,&\s}}]+',
        r'\1[REDACTED]',
        redacted,
    )
    return redacted


def _format_headers(headers: Any) -> str:
    return json.dumps(_redact_mapping(headers), ensure_ascii=False, separators=(",", ":"))


def _is_binary_response(content_type: str, url: str) -> bool:
    lowered = content_type.lower()
    if any(lowered.startswith(prefix) for prefix in _BINARY_CONTENT_PREFIXES):
        return True
    lowered_url = url.lower()
    return any(
        marker in lowered_url
        for marker in ("/media/", "/thumbnail", ".jpg", ".jpeg", ".png", ".mp4", ".m3u8")
    )


def create_blink_http_trace_config() -> TraceConfig:
    """Create an aiohttp TraceConfig that safely logs Blink HTTP traffic."""
    trace = TraceConfig()
    limit = _body_limit()

    async def on_request_start(session, ctx, params) -> None:
        try:
            ctx.started_at = time.perf_counter()
            ctx.request_method = params.method
            ctx.request_url = _redact_url(params.url)
            ctx.request_content_type = params.headers.get("Content-Type", "")
            ctx.request_body = bytearray()
            ctx.request_body_truncated = False
            LOGGER.info("→ %s %s", params.method, ctx.request_url)
            LOGGER.info("→ headers %s", _format_headers(params.headers))
        except Exception as error:
            LOGGER.warning("Blink HTTP trace request-start error: %s", error)

    async def on_request_chunk_sent(session, ctx, params) -> None:
        try:
            chunk = params.chunk or b""
            if not chunk or limit <= 0:
                return
            remaining = max(0, limit - len(ctx.request_body))
            if remaining:
                ctx.request_body.extend(chunk[:remaining])
            if len(chunk) > remaining:
                ctx.request_body_truncated = True
        except Exception as error:
            LOGGER.warning("Blink HTTP trace request-body error: %s", error)

    def log_request_body(ctx) -> None:
        body = bytes(getattr(ctx, "request_body", b""))
        if not body:
            return
        try:
            text = body.decode("utf-8", errors="replace")
            text = _redact_text(text, getattr(ctx, "request_content_type", ""))
            suffix = " …[truncated]" if getattr(ctx, "request_body_truncated", False) else ""
            LOGGER.info("→ body %s%s", text, suffix)
        except Exception as error:
            LOGGER.warning("Blink HTTP trace request-body formatting error: %s", error)

    async def on_request_end(session, ctx, params) -> None:
        try:
            log_request_body(ctx)
            elapsed_ms = int((time.perf_counter() - getattr(ctx, "started_at", time.perf_counter())) * 1000)
            response = params.response
            response_url = _redact_url(response.url)
            LOGGER.info(
                "← %s %s %s (%dms)",
                response.status,
                params.method,
                response_url,
                elapsed_ms,
            )
            LOGGER.info("← headers %s", _format_headers(response.headers))
        except Exception as error:
            LOGGER.warning("Blink HTTP trace request-end error: %s", error)

    async def on_request_redirect(session, ctx, params) -> None:
        try:
            response = params.response
            LOGGER.info(
                "↪ %s %s %s",
                response.status,
                params.method,
                _redact_url(params.url),
            )
            LOGGER.info("↪ headers %s", _format_headers(response.headers))
        except Exception as error:
            LOGGER.warning("Blink HTTP trace redirect error: %s", error)

    async def on_request_exception(session, ctx, params) -> None:
        try:
            log_request_body(ctx)
            elapsed_ms = int((time.perf_counter() - getattr(ctx, "started_at", time.perf_counter())) * 1000)
            LOGGER.info(
                "× %s %s (%dms): %s",
                params.method,
                _redact_url(params.url),
                elapsed_ms,
                params.exception,
            )
        except Exception as error:
            LOGGER.warning("Blink HTTP trace exception-hook error: %s", error)

    async def on_response_chunk_received(session, ctx, params) -> None:
        try:
            chunk = params.chunk or b""
            if not chunk:
                return

            # ClientResponse.read() normally emits this callback once with the
            # complete body. Avoid dumping media regardless of body size.
            response_url = _redact_url(params.url)
            content_type = ""
            # TraceResponseChunkReceivedParams does not expose response headers,
            # so use URL hints plus a text-decode heuristic here.
            if _is_binary_response(content_type, response_url):
                LOGGER.info("← body <binary payload: %d bytes>", len(chunk))
                return

            sample = chunk[:limit] if limit > 0 else b""
            if not sample:
                LOGGER.info("← body <%d bytes; body logging disabled>", len(chunk))
                return

            # Treat payload as binary if it contains NULs or cannot reasonably
            # be represented as text.
            if b"\x00" in sample:
                LOGGER.info("← body <binary payload: %d bytes>", len(chunk))
                return

            text = sample.decode("utf-8", errors="replace")
            replacement_ratio = text.count("\ufffd") / max(len(text), 1)
            if replacement_ratio > 0.05:
                LOGGER.info("← body <binary/non-text payload: %d bytes>", len(chunk))
                return

            text = _redact_text(text)
            suffix = " …[truncated]" if len(chunk) > len(sample) else ""
            LOGGER.info("← body %s%s", text, suffix)
        except Exception as error:
            LOGGER.warning("Blink HTTP trace response-body error: %s", error)

    trace.on_request_start.append(on_request_start)
    trace.on_request_chunk_sent.append(on_request_chunk_sent)
    trace.on_request_end.append(on_request_end)
    trace.on_request_redirect.append(on_request_redirect)
    trace.on_request_exception.append(on_request_exception)
    trace.on_response_chunk_received.append(on_response_chunk_received)
    return trace
