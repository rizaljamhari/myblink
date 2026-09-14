"""Read-only Blink cloud media/storage diagnostics.

This module probes Blink's current media APIs using the already-authenticated
session. The media list endpoint is a POST in Blink's API, but it is a retrieval
operation; this module never calls delete/update media endpoints.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from web_server import WebServer


_SENSITIVE_KEY_PARTS = (
    "password",
    "token",
    "authorization",
    "cookie",
    "secret",
    "email",
    "phone",
    "hardware_id",
    "device_id",
    "client_secret",
)

_MEDIA_DETAIL_KEYS = (
    "id",
    "camera_id",
    "camera_name",
    "device_id",
    "device_name",
    "device_type",
    "network_id",
    "network_name",
    "created_at",
    "updated_at",
    "viewed",
    "deleted",
    "type",
    "source",
)

_SIGNAL_KEY_PARTS = (
    "storage",
    "quota",
    "limit",
    "purge",
    "retention",
    "delete",
    "expire",
    "capacity",
    "count",
    "clip",
    "media",
)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _sanitize(value: Any, depth: int = 0) -> Any:
    """Recursively sanitize a diagnostic payload and cap its size."""
    if depth > 8:
        return "[max depth reached]"

    if value is None or isinstance(value, (bool, int, float, str)):
        return value

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= 200:
                result["__truncated__"] = f"{len(value) - 200} more field(s) omitted"
                break
            key_text = str(key)
            if _is_sensitive_key(key_text):
                continue
            result[key_text] = _sanitize(child, depth + 1)
        return result

    if isinstance(value, (list, tuple)):
        items = [_sanitize(child, depth + 1) for child in value[:100]]
        if len(value) > 100:
            items.append(f"[{len(value) - 100} more item(s) omitted]")
        return items

    return str(value)


def _collect_signals(
    value: Any,
    prefix: str = "",
    depth: int = 0,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Flatten quota/storage/retention-like primitive fields for quick reading."""
    if result is None:
        result = {}
    if depth > 8:
        return result

    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                continue
            path = f"{prefix}.{key_text}" if prefix else key_text
            lowered = key_text.lower()
            if any(part in lowered for part in _SIGNAL_KEY_PARTS):
                if child is None or isinstance(child, (bool, int, float, str)):
                    result[path] = child
                elif isinstance(child, (list, tuple)) and len(child) <= 20 and all(
                    item is None or isinstance(item, (bool, int, float, str))
                    for item in child
                ):
                    result[path] = list(child)
            if isinstance(child, (dict, list, tuple)):
                _collect_signals(child, path, depth + 1, result)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value[:100]):
            _collect_signals(child, f"{prefix}[{index}]", depth + 1, result)

    return result


def _safe_media_record(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"value": str(item)}

    result: dict[str, Any] = {}
    for key in _MEDIA_DETAIL_KEYS:
        # Device IDs are useful for matching targets but can be sensitive in a
        # shareable diagnostic, so omit the generic device_id specifically.
        if key == "device_id":
            continue
        if key in item:
            value = item[key]
            if value is None or isinstance(value, (bool, int, float, str)):
                result[key] = value
    return result


def _parse_created_at(item: dict[str, Any]) -> datetime | None:
    value = item.get("created_at")
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _summarize_media_response(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {
            "returned_count": 0,
            "response_type": type(payload).__name__,
            "response": _sanitize(payload),
        }

    raw_media = payload.get("media")
    media = raw_media if isinstance(raw_media, list) else []

    deleted_count = 0
    viewed_count = 0
    by_day: Counter[str] = Counter()
    by_camera: Counter[str] = Counter()
    timestamps: list[datetime] = []
    deleted_samples: list[dict[str, Any]] = []
    recent_samples: list[dict[str, Any]] = []

    for item in media:
        if not isinstance(item, dict):
            continue

        if item.get("deleted") is True:
            deleted_count += 1
            if len(deleted_samples) < 25:
                deleted_samples.append(_safe_media_record(item))

        if item.get("viewed") is True:
            viewed_count += 1

        created = _parse_created_at(item)
        if created is not None:
            timestamps.append(created)
            by_day[created.astimezone(timezone.utc).date().isoformat()] += 1

        camera_name = item.get("camera_name") or item.get("device_name")
        if camera_name:
            by_camera[str(camera_name)] += 1

        if len(recent_samples) < 25:
            recent_samples.append(_safe_media_record(item))

    response_metadata = {
        str(key): _sanitize(value)
        for key, value in payload.items()
        if key != "media"
    }

    return {
        "returned_count": len(media),
        "deleted_count": deleted_count,
        "viewed_count": viewed_count,
        "oldest_created_at": min(timestamps).isoformat() if timestamps else None,
        "newest_created_at": max(timestamps).isoformat() if timestamps else None,
        "by_day_utc": dict(sorted(by_day.items())),
        "by_camera": dict(by_camera.most_common()),
        "response_metadata": response_metadata,
        "response_signals": _collect_signals(response_metadata, "media_response"),
        "recent_samples": recent_samples,
        "deleted_samples": deleted_samples,
    }


async def _query_json(auth, *, url: str, method: str = "get", body: Any = None):
    try:
        headers = dict(auth.header or {})
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body)
        payload = await auth.query(
            url=url,
            data=data,
            headers=headers,
            reqtype=method,
        )
        if payload is None:
            return None, "Blink returned no payload"
        return payload, None
    except Exception as error:
        return None, str(error) or type(error).__name__


async def _collect_cloud_media_diagnostics(blink, hours: int) -> dict[str, Any]:
    auth = getattr(blink, "auth", None)
    urls = getattr(blink, "urls", None)
    base_url = getattr(urls, "base_url", None)
    account_id = getattr(blink, "account_id", None)

    if auth is None or not base_url or account_id is None:
        raise RuntimeError("Blink authentication session is unavailable")

    end_at = datetime.now(timezone.utc)
    start_at = end_at - timedelta(hours=hours)
    start_text = start_at.isoformat().replace("+00:00", "Z")
    end_text = end_at.isoformat().replace("+00:00", "Z")

    root = base_url.rstrip("/")
    media_settings_url = f"{root}/api/v4/accounts/{account_id}/media_settings"
    unwatched_url = f"{root}/api/v4/accounts/{account_id}/unwatched_media"
    media_query = urlencode({"start_time": start_text, "end_time": end_text})
    media_url = f"{root}/api/v4/accounts/{account_id}/media?{media_query}"

    settings, settings_error = await _query_json(
        auth,
        url=media_settings_url,
        method="get",
    )
    unwatched, unwatched_error = await _query_json(
        auth,
        url=unwatched_url,
        method="get",
    )
    media, media_error = await _query_json(
        auth,
        url=media_url,
        method="post",
        body={},
    )

    errors: dict[str, str] = {}
    if settings_error:
        errors["media_settings"] = settings_error
    if unwatched_error:
        errors["unwatched_media"] = unwatched_error
    if media_error:
        errors["media_list"] = media_error

    settings_safe = _sanitize(settings)
    unwatched_safe = _sanitize(unwatched)
    media_summary = _summarize_media_response(media)

    # Homescreen storage stats are useful context next to the dedicated media
    # API, especially because Blink exposes a compact `video_stats.storage`
    # value there.
    homescreen = getattr(blink, "homescreen", {}) or {}
    video_stats = (
        _sanitize(homescreen.get("video_stats", {}))
        if isinstance(homescreen, dict)
        else {}
    )

    return {
        "success": True,
        "read_only": True,
        "account_id": account_id,
        "region_id": getattr(auth, "region_id", None),
        "window": {
            "hours": hours,
            "start_at": start_text,
            "end_at": end_text,
        },
        "homescreen_video_stats": video_stats,
        "media_settings": settings_safe,
        "media_settings_signals": _collect_signals(settings_safe, "media_settings"),
        "unwatched_media": unwatched_safe,
        "media_list": media_summary,
        "errors": errors,
        "notes": [
            "The media-list POST is a read-only retrieval call in Blink's current API.",
            "returned_count is the number of clips Blink returned for this request, not necessarily the account's total stored clip count.",
            "purge_id and limit, when present, are server-provided fields and are shown without guessing their undocumented semantics.",
            "A deleted_count above zero means Blink explicitly returned clip records marked deleted in this diagnostic window.",
        ],
    }


_UI_PATCH_JS = r"""

// MyBlink Blink cloud media diagnostics UI.
(() => {
    function renderCloudMediaDiagnostics(data) {
        const result = document.getElementById('blinkCloudMediaDiagnosticsResult');
        if (!result) return;

        const list = data.media_list || {};
        const responseMeta = list.response_metadata || {};
        const errors = data.errors || {};
        const errorHtml = Object.entries(errors).map(([name, error]) =>
            `<div style="margin-top:0.35rem;color:var(--warning);">${app.escapeHtml(name)}: ${app.escapeHtml(String(error))}</div>`
        ).join('');

        const jsonBlock = (value, emptyText = 'No data returned by Blink.') => {
            if (value === null || value === undefined || (typeof value === 'object' && Object.keys(value).length === 0)) {
                return `<div style="color:var(--text-secondary);">${app.escapeHtml(emptyText)}</div>`;
            }
            return `<pre style="margin-top:0.5rem;max-height:420px;overflow:auto;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:0.8rem;font-size:0.8rem;">${app.escapeHtml(JSON.stringify(value, null, 2))}</pre>`;
        };

        result.innerHTML = `
            <div style="margin-top:1rem;">
                ${errorHtml}
                <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:0.75rem;margin-bottom:1rem;">
                    <div style="padding:0.8rem;border:1px solid var(--border);border-radius:8px;">
                        <div style="font-size:0.8rem;color:var(--text-secondary);">Returned Clips</div>
                        <strong>${app.escapeHtml(String(list.returned_count ?? 'Unknown'))}</strong>
                    </div>
                    <div style="padding:0.8rem;border:1px solid var(--border);border-radius:8px;">
                        <div style="font-size:0.8rem;color:var(--text-secondary);">Marked Deleted</div>
                        <strong>${app.escapeHtml(String(list.deleted_count ?? 'Unknown'))}</strong>
                    </div>
                    <div style="padding:0.8rem;border:1px solid var(--border);border-radius:8px;">
                        <div style="font-size:0.8rem;color:var(--text-secondary);">Server Limit</div>
                        <strong>${app.escapeHtml(String(responseMeta.limit ?? 'Not returned'))}</strong>
                    </div>
                    <div style="padding:0.8rem;border:1px solid var(--border);border-radius:8px;">
                        <div style="font-size:0.8rem;color:var(--text-secondary);">Purge ID</div>
                        <strong>${app.escapeHtml(String(responseMeta.purge_id ?? 'Not returned'))}</strong>
                    </div>
                </div>

                <details open style="margin-top:1rem;">
                    <summary style="cursor:pointer;font-weight:600;">Homescreen Video Stats</summary>
                    ${jsonBlock(data.homescreen_video_stats)}
                </details>

                <details open style="margin-top:1rem;">
                    <summary style="cursor:pointer;font-weight:600;">Media Settings / Quota Signals</summary>
                    ${jsonBlock(data.media_settings_signals)}
                    ${jsonBlock(data.media_settings, 'Blink did not return media settings.')}
                </details>

                <details style="margin-top:1rem;">
                    <summary style="cursor:pointer;font-weight:600;">Unwatched Media</summary>
                    ${jsonBlock(data.unwatched_media)}
                </details>

                <details open style="margin-top:1rem;">
                    <summary style="cursor:pointer;font-weight:600;">Clip List Summary</summary>
                    ${jsonBlock({
                        window: data.window,
                        oldest_created_at: list.oldest_created_at,
                        newest_created_at: list.newest_created_at,
                        returned_count: list.returned_count,
                        deleted_count: list.deleted_count,
                        viewed_count: list.viewed_count,
                        by_day_utc: list.by_day_utc,
                        by_camera: list.by_camera,
                        response_metadata: list.response_metadata,
                        response_signals: list.response_signals
                    })}
                </details>

                <details style="margin-top:1rem;">
                    <summary style="cursor:pointer;font-weight:600;">Deleted Clip Samples</summary>
                    ${jsonBlock(list.deleted_samples, 'No clips marked deleted were returned in this window.')}
                </details>

                <details style="margin-top:1rem;">
                    <summary style="cursor:pointer;font-weight:600;">Recent Clip Samples</summary>
                    ${jsonBlock(list.recent_samples)}
                </details>

                <details style="margin-top:1rem;">
                    <summary style="cursor:pointer;font-weight:600;">Raw Cloud Media Diagnostic JSON</summary>
                    ${jsonBlock(data)}
                </details>
            </div>
        `;
    }

    function installCloudMediaDiagnostics(instance) {
        const container = document.getElementById('settingsPage');
        if (!container || document.getElementById('blinkCloudMediaDiagnosticsSection')) return;

        const section = document.createElement('div');
        section.id = 'blinkCloudMediaDiagnosticsSection';
        section.className = 'settings-section';
        section.innerHTML = `
            <div class="settings-title">Blink Cloud Media Diagnostics</div>
            <p style="color:var(--text-secondary);margin-bottom:0.75rem;">
                Read-only inspection of Blink media settings, storage signals, and recent cloud clip metadata.
            </p>
            <div style="display:flex;gap:0.75rem;align-items:end;flex-wrap:wrap;">
                <label style="display:flex;flex-direction:column;gap:0.25rem;">
                    <span style="font-size:0.8rem;color:var(--text-secondary);">Diagnostic window</span>
                    <select id="blinkCloudMediaWindow" class="form-control">
                        <option value="24">Last 24 hours</option>
                        <option value="48" selected>Last 48 hours</option>
                        <option value="72">Last 72 hours</option>
                        <option value="168">Last 7 days</option>
                    </select>
                </label>
                <button type="button" class="btn btn-secondary" id="runBlinkCloudMediaDiagnosticsBtn">Run Cloud Diagnostics</button>
            </div>
            <div id="blinkCloudMediaDiagnosticsResult" style="margin-top:0.5rem;color:var(--text-secondary);">
                Diagnostics have not been run yet.
            </div>
        `;
        container.appendChild(section);

        document.getElementById('runBlinkCloudMediaDiagnosticsBtn')?.addEventListener('click', async () => {
            const button = document.getElementById('runBlinkCloudMediaDiagnosticsBtn');
            const result = document.getElementById('blinkCloudMediaDiagnosticsResult');
            const select = document.getElementById('blinkCloudMediaWindow');
            const hours = select?.value || '48';

            if (button) {
                button.disabled = true;
                button.textContent = 'Checking Blink...';
            }
            if (result) result.textContent = 'Reading Blink media settings and recent cloud clip metadata...';

            try {
                const response = await fetch(`/api/blink/cloud-media-diagnostics?hours=${encodeURIComponent(hours)}`);
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || 'Cloud media diagnostics failed');
                instance._blinkCloudMediaDiagnostics = data;
                renderCloudMediaDiagnostics(data);
            } catch (error) {
                if (result) {
                    result.innerHTML = `<div style="color:var(--error);">Diagnostics failed: ${app.escapeHtml(error.message)}</div>`;
                }
            } finally {
                if (button) {
                    button.disabled = false;
                    button.textContent = 'Run Cloud Diagnostics';
                }
            }
        });

        if (instance._blinkCloudMediaDiagnostics) {
            renderCloudMediaDiagnostics(instance._blinkCloudMediaDiagnostics);
        }
    }

    if (typeof MyBlinkApp !== 'undefined') {
        const previousRenderSettingsPage = MyBlinkApp.prototype.renderSettingsPage;
        if (previousRenderSettingsPage) {
            MyBlinkApp.prototype.renderSettingsPage = function(...args) {
                const result = previousRenderSettingsPage.apply(this, args);
                setTimeout(() => installCloudMediaDiagnostics(this), 0);
                return result;
            };
        }
    }
})();
"""


def _find_app_js_endpoint(app) -> str | None:
    for rule in app.url_map.iter_rules():
        if rule.rule == "/static/app.js" and "GET" in rule.methods:
            return rule.endpoint
    return None


def apply_blink_cloud_media_diagnostics() -> None:
    """Register cloud-media diagnostics API and append its Settings UI."""
    original_init = WebServer.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)

        from flask import jsonify, request

        if "blink_cloud_media_diagnostics_api" not in self.app.view_functions:

            def cloud_media_diagnostics():
                handler = getattr(self.myblink_app, "blink_handler", None)
                blink = getattr(handler, "blink", None) if handler else None
                event_loop = getattr(self.myblink_app, "_event_loop", None)

                if blink is None or event_loop is None:
                    return jsonify({"error": "Blink is not initialized"}), 503

                try:
                    hours = int(request.args.get("hours", "48"))
                except ValueError:
                    return jsonify({"error": "hours must be an integer"}), 400

                if hours not in (24, 48, 72, 168):
                    return jsonify({"error": "hours must be one of 24, 48, 72, 168"}), 400

                try:
                    future = asyncio.run_coroutine_threadsafe(
                        _collect_cloud_media_diagnostics(blink, hours),
                        event_loop,
                    )
                    data = future.result(timeout=45)
                    return jsonify(data)
                except Exception as error:
                    self.logger.error(
                        "Blink cloud media diagnostics failed: %s",
                        error,
                        exc_info=True,
                    )
                    return jsonify({"error": str(error)}), 500

            self.app.add_url_rule(
                "/api/blink/cloud-media-diagnostics",
                endpoint="blink_cloud_media_diagnostics_api",
                view_func=self._require_auth(cloud_media_diagnostics),
                methods=["GET"],
            )

        app_js_endpoint = _find_app_js_endpoint(self.app)
        if not app_js_endpoint:
            self.logger.warning("app.js route not found; cloud media diagnostics UI skipped")
            return

        original_view = self.app.view_functions[app_js_endpoint]

        def patched_app_js(*view_args, **view_kwargs):
            response = self.app.make_response(original_view(*view_args, **view_kwargs))
            response.direct_passthrough = False
            javascript = response.get_data(as_text=True)
            if "MyBlink Blink cloud media diagnostics UI" not in javascript:
                javascript += _UI_PATCH_JS
                response.set_data(javascript)
                response.headers["Content-Length"] = str(len(response.get_data()))
            return response

        self.app.view_functions[app_js_endpoint] = patched_app_js

    WebServer.__init__ = patched_init
