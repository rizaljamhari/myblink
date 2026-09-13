"""Expose Blink OAuth rate-limit state to the configuration UI.

This patch keeps the main web server untouched while adding structured 429
responses, an in-page countdown, and a guard that disables repeated login
attempts until Blink's reported reset time has elapsed.
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timedelta
from typing import Any

from compatibility_fixes import BlinkRateLimitError
from modules.blink_handler import BlinkHandler
from web_server import WebServer


_RATE_LIMIT_UI = r"""
<script id="blink-rate-limit-ui">
(() => {
    const originalFetch = window.fetch.bind(window);
    let countdownTimer = null;

    function formatRemaining(totalSeconds) {
        const seconds = Math.max(0, Math.floor(totalSeconds));
        const days = Math.floor(seconds / 86400);
        const hours = Math.floor((seconds % 86400) / 3600);
        const minutes = Math.floor((seconds % 3600) / 60);
        const secs = seconds % 60;
        const parts = [];
        if (days) parts.push(`${days}d`);
        if (hours || days) parts.push(`${hours}h`);
        if (minutes || hours || days) parts.push(`${minutes}m`);
        parts.push(`${secs}s`);
        return parts.join(' ');
    }

    function renderRateLimit(data) {
        if (!data || !data.rate_limited) return;

        const errorBox = document.getElementById('errorMessage');
        const submitButton = document.getElementById('submitBtn');
        if (!errorBox || !submitButton) return;

        let retryAtMs = data.retry_at ? Date.parse(data.retry_at) : NaN;
        if (!Number.isFinite(retryAtMs) && Number.isFinite(Number(data.retry_after))) {
            retryAtMs = Date.now() + Number(data.retry_after) * 1000;
        }
        if (!Number.isFinite(retryAtMs)) return;

        if (countdownTimer) {
            clearInterval(countdownTimer);
            countdownTimer = null;
        }

        const retryAtText = new Date(retryAtMs).toLocaleString([], {
            year: 'numeric',
            month: 'short',
            day: 'numeric',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit'
        });

        function update() {
            const remaining = Math.max(0, Math.ceil((retryAtMs - Date.now()) / 1000));

            errorBox.replaceChildren();
            const title = document.createElement('strong');
            title.textContent = 'Blink 2FA rate limit exceeded.';
            errorBox.appendChild(title);

            const retryLine = document.createElement('div');
            retryLine.style.marginTop = '6px';
            retryLine.textContent = `You can try again after: ${retryAtText}`;
            errorBox.appendChild(retryLine);

            const countdownLine = document.createElement('div');
            countdownLine.style.marginTop = '4px';
            countdownLine.textContent = remaining > 0
                ? `Time remaining: ${formatRemaining(remaining)}`
                : 'The reported rate-limit window has expired. You can try again.';
            errorBox.appendChild(countdownLine);

            if (data.cause) {
                const causeLine = document.createElement('div');
                causeLine.style.marginTop = '4px';
                causeLine.style.opacity = '0.8';
                causeLine.style.fontSize = '12px';
                causeLine.textContent = `Blink reason: ${data.cause}`;
                errorBox.appendChild(causeLine);
            }

            errorBox.classList.add('show');

            if (remaining > 0) {
                submitButton.disabled = true;
                submitButton.textContent = `Try again in ${formatRemaining(remaining)}`;
            } else {
                submitButton.disabled = false;
                submitButton.textContent = 'Connect to Blink';
                if (countdownTimer) {
                    clearInterval(countdownTimer);
                    countdownTimer = null;
                }
            }
        }

        update();
        if (retryAtMs > Date.now()) {
            countdownTimer = setInterval(update, 1000);
        }
    }

    window.fetch = async function(input, init) {
        const response = await originalFetch(input, init);
        const url = typeof input === 'string' ? input : (input && input.url) || '';

        if (url.includes('/api/config/save') || url.includes('/api/config/current')) {
            response.clone().json().then(data => {
                if (data && data.rate_limited) {
                    // Let the page's existing submit handler finish first, then
                    // replace its generic error with the structured countdown.
                    setTimeout(() => renderRateLimit(data), 50);
                }
            }).catch(() => {});
        }

        return response;
    };

    document.addEventListener('DOMContentLoaded', async () => {
        try {
            const response = await originalFetch('/api/config/current', { cache: 'no-store' });
            if (!response.ok) return;
            const data = await response.json();
            if (data && data.rate_limited) renderRateLimit(data);
        } catch (_) {
            // Existing configuration UI handles normal connectivity errors.
        }
    });
})();
</script>
"""


def _find_rate_limit_error(error: BaseException) -> BlinkRateLimitError | None:
    """Walk an exception chain and return the Blink rate-limit error, if any."""
    current: BaseException | None = error
    seen: set[int] = set()

    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, BlinkRateLimitError):
            return current
        current = current.__cause__ or current.__context__

    return None


def _extract_cause(error: BlinkRateLimitError) -> str:
    cause = getattr(error, "error_cause", None)
    if cause:
        return str(cause)

    match = re.search(r"cause=([A-Za-z0-9_.-]+)", str(error))
    return match.group(1) if match else "rate_limit_exceeded"


def _patch_blink_rate_limit_state() -> None:
    """Remember Blink's active lockout window on the current handler."""
    original_initialize = BlinkHandler.initialize

    async def initialize(self, *args, **kwargs):
        try:
            result = await original_initialize(self, *args, **kwargs)
        except Exception as error:
            rate_error = _find_rate_limit_error(error)
            if rate_error is not None:
                try:
                    seconds = max(0, int(float(rate_error.retry_after or 0)))
                except (TypeError, ValueError, OverflowError):
                    seconds = 0

                if seconds > 0:
                    retry_at = datetime.now().astimezone() + timedelta(seconds=seconds)
                    self._blink_rate_limit_info = {
                        "rate_limited": True,
                        "retry_after": seconds,
                        "retry_at": retry_at.isoformat(),
                        "retry_epoch": retry_at.timestamp(),
                        "cause": _extract_cause(rate_error),
                        "description": "Blink has temporarily blocked additional 2FA login attempts.",
                    }
            raise
        else:
            self._blink_rate_limit_info = None
            return result

    BlinkHandler.initialize = initialize


def _active_rate_limit(myblink_app: Any) -> dict[str, Any] | None:
    handler = getattr(myblink_app, "blink_handler", None)
    info = getattr(handler, "_blink_rate_limit_info", None) if handler else None
    if not info:
        return None

    retry_epoch = float(info.get("retry_epoch") or 0)
    remaining = max(0, math.ceil(retry_epoch - time.time()))
    if remaining <= 0:
        handler._blink_rate_limit_info = None
        return None

    current = dict(info)
    current["retry_after"] = remaining
    return current


def _patch_web_server() -> None:
    """Add structured API responses and inject countdown UI into /configure."""
    original_init = WebServer.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)

        @self.app.after_request
        def add_rate_limit_state(response):
            from flask import request

            info = _active_rate_limit(self.myblink_app)

            if request.path == "/api/config/save" and info and response.status_code >= 400:
                payload = {
                    "error": "Blink 2FA rate limit exceeded.",
                    "rate_limited": True,
                    "retry_after": info["retry_after"],
                    "retry_at": info["retry_at"],
                    "cause": info.get("cause"),
                    "description": info.get("description"),
                }
                response.set_data(json.dumps(payload))
                response.status_code = 429
                response.content_type = "application/json"

            elif request.path == "/api/config/current" and info:
                try:
                    payload = response.get_json(silent=True) or {}
                    if isinstance(payload, dict):
                        payload.update(
                            {
                                "rate_limited": True,
                                "retry_after": info["retry_after"],
                                "retry_at": info["retry_at"],
                                "cause": info.get("cause"),
                            }
                        )
                        response.set_data(json.dumps(payload))
                        response.content_type = "application/json"
                except Exception:
                    pass

            if request.path == "/configure" and response.status_code == 200:
                content_type = response.headers.get("Content-Type", "")
                if "text/html" in content_type:
                    html = response.get_data(as_text=True)
                    if "blink-rate-limit-ui" not in html:
                        if "</body>" in html:
                            html = html.replace("</body>", _RATE_LIMIT_UI + "\n</body>")
                        else:
                            html += _RATE_LIMIT_UI
                        response.set_data(html)
                        response.headers["Content-Length"] = str(len(response.get_data()))

            return response

    WebServer.__init__ = patched_init


def apply_web_rate_limit_ui() -> None:
    """Enable structured Blink rate-limit reporting in the web UI."""
    _patch_blink_rate_limit_state()
    _patch_web_server()
