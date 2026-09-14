"""Read-only Blink account diagnostics exposed in the MyBlink settings UI.

This module deliberately avoids mutating Blink account state. It queries known
read-only account/subscription endpoints and inspects already-loaded BlinkPy
homescreen/sync metadata for subscription, legacy, storage, and retention
signals that can help explain unusual account entitlements.
"""

from __future__ import annotations

import asyncio
from typing import Any

from web_server import WebServer


_RELEVANT_KEY_PARTS = (
    "legacy",
    "subscription",
    "trial",
    "feature_plan",
    "plan_id",
    "entitlement",
    "storage",
    "auto_delete",
    "auto_purge",
    "retention",
    "video_count",
    "video_history_count",
    "cloud",
)

_STRUCTURED_CONTAINER_KEY_PARTS = (
    "subscription",
    "entitlement",
    "feature_plan",
    "plans",
    "trial",
)

_ACCOUNT_OPTION_SIGNAL_PARTS = (
    "legacy",
    "subs",
    "trial",
    "plan",
    "storage",
    "clip_list_limit",
)

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
    "first_name",
    "last_name",
    "full_name",
    "address",
    "postal",
)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _is_relevant_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _RELEVANT_KEY_PARTS)


def _safe_primitive(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        if len(value) <= 20 and all(
            item is None or isinstance(item, (bool, int, float, str)) for item in value
        ):
            return list(value)
    return None


def _sanitize_structure(value: Any, depth: int = 0) -> Any:
    """Recursively sanitize diagnostic payloads while preserving useful structure."""
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
            result[key_text] = _sanitize_structure(child, depth + 1)
        return result

    if isinstance(value, (list, tuple)):
        items = [_sanitize_structure(child, depth + 1) for child in value[:100]]
        if len(value) > 100:
            items.append(f"[{len(value) - 100} more item(s) omitted]")
        return items

    return str(value)


def _safe_options(options: Any) -> dict[str, Any]:
    if not isinstance(options, dict):
        return {}

    result: dict[str, Any] = {}
    for key, value in options.items():
        if _is_sensitive_key(str(key)):
            continue
        safe_value = _safe_primitive(value)
        if safe_value is not None or value is None:
            result[str(key)] = safe_value
    return result


def _collect_relevant_signals(
    value: Any,
    prefix: str = "homescreen",
    depth: int = 0,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Flatten relevant subscription/storage fields without exposing raw payloads."""
    if result is None:
        result = {}
    if depth > 7:
        return result

    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                continue
            path = f"{prefix}.{key_text}" if prefix else key_text

            if _is_relevant_key(key_text):
                safe_value = _safe_primitive(child)
                if safe_value is not None or child is None:
                    result[path] = safe_value

            if isinstance(child, (dict, list, tuple)):
                _collect_relevant_signals(child, path, depth + 1, result)

    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value[:100]):
            _collect_relevant_signals(child, f"{prefix}[{index}]", depth + 1, result)

    return result


def _collect_structured_containers(
    value: Any,
    prefix: str = "homescreen",
    depth: int = 0,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Preserve whole sanitized plan/subscription containers and their child fields."""
    if result is None:
        result = {}
    if depth > 7:
        return result

    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                continue
            path = f"{prefix}.{key_text}" if prefix else key_text
            lowered = key_text.lower()

            if isinstance(child, (dict, list, tuple)) and any(
                part in lowered for part in _STRUCTURED_CONTAINER_KEY_PARTS
            ):
                result[path] = _sanitize_structure(child)

            if isinstance(child, (dict, list, tuple)):
                _collect_structured_containers(child, path, depth + 1, result)

    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value[:100]):
            _collect_structured_containers(
                child,
                f"{prefix}[{index}]",
                depth + 1,
                result,
            )

    return result


def _sync_summaries(blink) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    syncs = getattr(blink, "sync", {}) or {}

    for sync_name, sync in syncs.items():
        row: dict[str, Any] = {"name": str(sync_name)}
        for attr in (
            "network_id",
            "feature_plan_id",
            "storage_used",
            "storage_total",
            "video_count",
            "video_history_count",
        ):
            value = _safe_primitive(getattr(sync, attr, None))
            if value is not None:
                row[attr] = value
        summaries.append(row)

    return summaries


async def _query_read_only_json(auth, url: str) -> tuple[Any, str | None]:
    """Query a Blink GET endpoint and return a sanitized payload plus any error."""
    try:
        payload = await auth.query(
            url=url,
            headers=auth.header,
            reqtype="get",
        )
        if payload is None:
            return None, "Blink returned no payload"
        if not isinstance(payload, (dict, list, tuple)):
            return _sanitize_structure(payload), (
                f"Unexpected response type: {type(payload).__name__}"
            )
        return _sanitize_structure(payload), None
    except Exception as error:  # diagnostics must never disrupt the application
        return None, str(error) or type(error).__name__


async def _collect_subscription_api(blink, base_url: str | None) -> dict[str, Any]:
    """Query current Blink read-only subscription endpoints from Android API evidence."""
    auth = getattr(blink, "auth", None)
    account_id = getattr(blink, "account_id", None)

    result: dict[str, Any] = {
        "access": None,
        "entitlements": None,
        "plans": None,
        "errors": {},
    }

    if auth is None or not base_url or account_id is None:
        result["errors"]["subscription_api"] = (
            "Blink authentication session, base URL, or account ID is unavailable"
        )
        return result

    root = base_url.rstrip("/")
    endpoints = {
        "access": f"{root}/api/v1/accounts/{account_id}/access",
        "entitlements": (
            f"{root}/api/v2/accounts/{account_id}/subscriptions/entitlements"
        ),
        "plans": f"{root}/api/v3/accounts/{account_id}/subscriptions/plans",
    }

    for name, url in endpoints.items():
        payload, error = await _query_read_only_json(auth, url)
        result[name] = payload
        if error:
            result["errors"][name] = error

    return result


async def _collect_diagnostics(blink) -> dict[str, Any]:
    auth = getattr(blink, "auth", None)
    urls = getattr(blink, "urls", None)
    base_url = getattr(urls, "base_url", None)

    account = {
        "account_id": _safe_primitive(getattr(blink, "account_id", None)),
        "region_id": _safe_primitive(getattr(auth, "region_id", None)),
        "host": _safe_primitive(getattr(auth, "host", None)),
        "base_url": _safe_primitive(base_url),
    }

    account_options: dict[str, Any] = {}
    account_options_error: str | None = None

    if auth is None or not base_url:
        account_options_error = "Blink authentication session is unavailable"
    else:
        try:
            options_url = f"{base_url.rstrip('/')}/api/v1/account/options"
            options = await auth.query(
                url=options_url,
                headers=auth.header,
                reqtype="get",
            )
            if isinstance(options, dict):
                account_options = _safe_options(options)
            elif options is None:
                account_options_error = "Blink returned no account/options payload"
            else:
                account_options_error = (
                    f"Unexpected account/options response type: {type(options).__name__}"
                )
        except Exception as error:  # diagnostics must not break the settings page
            account_options_error = str(error) or type(error).__name__

    subscription_api = await _collect_subscription_api(blink, base_url)

    homescreen = getattr(blink, "homescreen", {}) or {}
    homescreen_signals = _collect_relevant_signals(homescreen)
    structured_homescreen = _collect_structured_containers(homescreen)
    homescreen_top_level_keys = (
        sorted(str(key) for key in homescreen.keys())
        if isinstance(homescreen, dict)
        else []
    )

    account_option_signals = {
        key: value
        for key, value in account_options.items()
        if any(part in key.lower() for part in _ACCOUNT_OPTION_SIGNAL_PARTS)
    }

    # Surface the most useful evidence separately for quick reading.
    legacy_account_mini = account_options.get("legacy_account_mini")
    feature_plan_signals = {
        path: value
        for path, value in homescreen_signals.items()
        if "feature_plan" in path.lower() or "plan_id" in path.lower()
    }
    retention_signals = {
        path: value
        for path, value in homescreen_signals.items()
        if any(
            part in path.lower()
            for part in ("auto_delete", "auto_purge", "retention")
        )
    }
    storage_signals = {
        path: value
        for path, value in homescreen_signals.items()
        if any(
            part in path.lower()
            for part in ("storage", "video_count", "video_history_count")
        )
    }
    subscription_signals = {
        path: value
        for path, value in homescreen_signals.items()
        if any(
            part in path.lower()
            for part in ("subscription", "trial", "entitlement", "cloud")
        )
    }

    return {
        "success": True,
        "read_only": True,
        "account": account,
        "account_options": account_options,
        "account_option_signals": account_option_signals,
        "account_options_error": account_options_error,
        "legacy_account_mini": legacy_account_mini,
        "subscription_api": subscription_api,
        "feature_plan_signals": feature_plan_signals,
        "subscription_signals": subscription_signals,
        "structured_homescreen": structured_homescreen,
        "retention_signals": retention_signals,
        "storage_signals": storage_signals,
        "homescreen_signals": homescreen_signals,
        "homescreen_top_level_keys": homescreen_top_level_keys,
        "sync_modules": _sync_summaries(blink),
    }


_UI_PATCH_JS = r"""

// MyBlink Blink account diagnostics UI.
(() => {
    function escapeValue(value) {
        if (value === null || value === undefined) return 'Unknown';
        if (typeof value === 'boolean') return value ? 'Yes' : 'No';
        if (typeof value === 'object') return JSON.stringify(value);
        return String(value);
    }

    function renderKeyValueRows(values) {
        const entries = Object.entries(values || {});
        if (!entries.length) {
            return '<div style="color: var(--text-secondary);">No matching fields returned by Blink.</div>';
        }
        return entries.map(([key, value]) => `
            <div style="display:grid;grid-template-columns:minmax(180px, 1fr) minmax(180px, 1fr);gap:0.75rem;padding:0.5rem 0;border-bottom:1px solid var(--border);">
                <code style="overflow-wrap:anywhere;">${app.escapeHtml(key)}</code>
                <span style="overflow-wrap:anywhere;">${app.escapeHtml(escapeValue(value))}</span>
            </div>
        `).join('');
    }

    function renderJson(value, emptyText = 'No data returned by Blink.') {
        if (value === null || value === undefined) {
            return `<div style="color:var(--text-secondary);">${app.escapeHtml(emptyText)}</div>`;
        }
        if (typeof value === 'object' && Object.keys(value).length === 0) {
            return `<div style="color:var(--text-secondary);">${app.escapeHtml(emptyText)}</div>`;
        }
        return `<pre style="margin-top:0.5rem;max-height:360px;overflow:auto;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:0.8rem;font-size:0.8rem;">${app.escapeHtml(JSON.stringify(value, null, 2))}</pre>`;
    }

    function renderDiagnostics(data) {
        const result = document.getElementById('blinkAccountDiagnosticsResult');
        if (!result) return;

        const legacy = data.legacy_account_mini;
        const legacyLabel = legacy === true
            ? 'Yes — Blink returned legacy_account_mini=true'
            : legacy === false
                ? 'No — Blink returned legacy_account_mini=false'
                : 'Unknown — flag was not returned';

        const account = data.account || {};
        const subscriptionApi = data.subscription_api || {};
        const subscriptionErrors = subscriptionApi.errors || {};
        const optionsError = data.account_options_error
            ? `<div style="margin-top:0.5rem;color:var(--warning);">account/options: ${app.escapeHtml(data.account_options_error)}</div>`
            : '';
        const subscriptionErrorHtml = Object.entries(subscriptionErrors).map(([name, error]) =>
            `<div style="margin-top:0.35rem;color:var(--warning);">${app.escapeHtml(name)}: ${app.escapeHtml(String(error))}</div>`
        ).join('');

        result.innerHTML = `
            <div style="margin-top:1rem;">
                <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:0.75rem;margin-bottom:1rem;">
                    <div style="padding:0.8rem;border:1px solid var(--border);border-radius:8px;">
                        <div style="font-size:0.8rem;color:var(--text-secondary);">Account ID</div>
                        <strong>${app.escapeHtml(escapeValue(account.account_id))}</strong>
                    </div>
                    <div style="padding:0.8rem;border:1px solid var(--border);border-radius:8px;">
                        <div style="font-size:0.8rem;color:var(--text-secondary);">Region</div>
                        <strong>${app.escapeHtml(escapeValue(account.region_id || account.host))}</strong>
                    </div>
                    <div style="padding:0.8rem;border:1px solid var(--border);border-radius:8px;">
                        <div style="font-size:0.8rem;color:var(--text-secondary);">Legacy Account Flag</div>
                        <strong>${app.escapeHtml(legacyLabel)}</strong>
                    </div>
                </div>

                ${optionsError}
                ${subscriptionErrorHtml}

                <details open style="margin-top:1rem;">
                    <summary style="cursor:pointer;font-weight:600;">Subscription API — Plans</summary>
                    ${renderJson(subscriptionApi.plans, 'Blink did not return a subscription-plans payload.')}
                </details>

                <details open style="margin-top:1rem;">
                    <summary style="cursor:pointer;font-weight:600;">Subscription API — Entitlements</summary>
                    ${renderJson(subscriptionApi.entitlements, 'Blink did not return an entitlements payload.')}
                </details>

                <details style="margin-top:1rem;">
                    <summary style="cursor:pointer;font-weight:600;">Subscription API — Account Access</summary>
                    ${renderJson(subscriptionApi.access, 'Blink did not return an account-access payload.')}
                </details>

                <details open style="margin-top:1rem;">
                    <summary style="cursor:pointer;font-weight:600;">Account Subscription / Trial Flags</summary>
                    <div style="margin-top:0.5rem;">${renderKeyValueRows(data.account_option_signals)}</div>
                </details>

                <details style="margin-top:1rem;">
                    <summary style="cursor:pointer;font-weight:600;">All Account Options</summary>
                    <div style="margin-top:0.5rem;">${renderKeyValueRows(data.account_options)}</div>
                </details>

                <details open style="margin-top:1rem;">
                    <summary style="cursor:pointer;font-weight:600;">Structured Homescreen Plan / Subscription Objects</summary>
                    ${renderJson(data.structured_homescreen, 'No structured plan/subscription containers were present in the loaded homescreen.')}
                </details>

                <details style="margin-top:1rem;">
                    <summary style="cursor:pointer;font-weight:600;">Feature / Plan Signals</summary>
                    <div style="margin-top:0.5rem;">${renderKeyValueRows(data.feature_plan_signals)}</div>
                </details>

                <details style="margin-top:1rem;">
                    <summary style="cursor:pointer;font-weight:600;">Subscription / Trial Signals</summary>
                    <div style="margin-top:0.5rem;">${renderKeyValueRows(data.subscription_signals)}</div>
                </details>

                <details style="margin-top:1rem;">
                    <summary style="cursor:pointer;font-weight:600;">Retention Signals</summary>
                    <div style="margin-top:0.5rem;">${renderKeyValueRows(data.retention_signals)}</div>
                </details>

                <details style="margin-top:1rem;">
                    <summary style="cursor:pointer;font-weight:600;">Storage / Video Count Signals</summary>
                    <div style="margin-top:0.5rem;">${renderKeyValueRows(data.storage_signals)}</div>
                </details>

                <details style="margin-top:1rem;">
                    <summary style="cursor:pointer;font-weight:600;">Homescreen Top-Level Keys</summary>
                    ${renderJson(data.homescreen_top_level_keys)}
                </details>

                <details style="margin-top:1rem;">
                    <summary style="cursor:pointer;font-weight:600;">Raw Diagnostic JSON</summary>
                    <pre style="margin-top:0.5rem;max-height:520px;overflow:auto;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:0.8rem;font-size:0.8rem;">${app.escapeHtml(JSON.stringify(data, null, 2))}</pre>
                </details>
            </div>
        `;
    }

    function installDiagnosticsSection(instance) {
        const container = document.getElementById('settingsPage');
        if (!container || document.getElementById('blinkAccountDiagnosticsSection')) return;

        const section = document.createElement('div');
        section.id = 'blinkAccountDiagnosticsSection';
        section.className = 'settings-section';
        section.innerHTML = `
            <div class="settings-title">Blink Account Diagnostics</div>
            <p style="color:var(--text-secondary);margin-bottom:0.75rem;">
                Read-only inspection of Blink account, subscription, entitlement, and storage metadata. No account settings are changed.
            </p>
            <button type="button" class="btn btn-secondary" id="runBlinkAccountDiagnosticsBtn">Run Diagnostics</button>
            <div id="blinkAccountDiagnosticsResult" style="margin-top:0.5rem;color:var(--text-secondary);">
                Diagnostics have not been run yet.
            </div>
        `;
        container.appendChild(section);

        document.getElementById('runBlinkAccountDiagnosticsBtn')?.addEventListener('click', async () => {
            const button = document.getElementById('runBlinkAccountDiagnosticsBtn');
            const result = document.getElementById('blinkAccountDiagnosticsResult');
            if (button) {
                button.disabled = true;
                button.textContent = 'Checking Blink...';
            }
            if (result) result.textContent = 'Reading account and subscription metadata from Blink...';

            try {
                const response = await fetch('/api/blink/account-diagnostics');
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || 'Diagnostics request failed');
                instance._blinkAccountDiagnostics = data;
                renderDiagnostics(data);
            } catch (error) {
                if (result) {
                    result.innerHTML = `<div style="color:var(--error);">Diagnostics failed: ${app.escapeHtml(error.message)}</div>`;
                }
            } finally {
                if (button) {
                    button.disabled = false;
                    button.textContent = 'Run Diagnostics';
                }
            }
        });

        if (instance._blinkAccountDiagnostics) {
            renderDiagnostics(instance._blinkAccountDiagnostics);
        }
    }

    if (typeof MyBlinkApp !== 'undefined') {
        const previousRenderSettingsPage = MyBlinkApp.prototype.renderSettingsPage;
        if (previousRenderSettingsPage) {
            MyBlinkApp.prototype.renderSettingsPage = function(...args) {
                const result = previousRenderSettingsPage.apply(this, args);
                setTimeout(() => installDiagnosticsSection(this), 0);
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


def apply_blink_account_diagnostics() -> None:
    """Register the diagnostics API and append the Settings UI helper."""
    original_init = WebServer.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)

        from flask import jsonify

        if "blink_account_diagnostics_api" not in self.app.view_functions:

            def account_diagnostics():
                handler = getattr(self.myblink_app, "blink_handler", None)
                blink = getattr(handler, "blink", None) if handler else None
                event_loop = getattr(self.myblink_app, "_event_loop", None)

                if blink is None or event_loop is None:
                    return jsonify({"error": "Blink is not initialized"}), 503

                try:
                    future = asyncio.run_coroutine_threadsafe(
                        _collect_diagnostics(blink),
                        event_loop,
                    )
                    data = future.result(timeout=45)
                    return jsonify(data)
                except Exception as error:
                    self.logger.error(
                        "Blink account diagnostics failed: %s",
                        error,
                        exc_info=True,
                    )
                    return jsonify({"error": str(error)}), 500

            self.app.add_url_rule(
                "/api/blink/account-diagnostics",
                endpoint="blink_account_diagnostics_api",
                view_func=self._require_auth(account_diagnostics),
                methods=["GET"],
            )

        app_js_endpoint = _find_app_js_endpoint(self.app)
        if not app_js_endpoint:
            self.logger.warning("app.js route not found; account diagnostics UI skipped")
            return

        original_view = self.app.view_functions[app_js_endpoint]

        def patched_app_js(*view_args, **view_kwargs):
            response = self.app.make_response(original_view(*view_args, **view_kwargs))
            response.direct_passthrough = False
            javascript = response.get_data(as_text=True)
            if "MyBlink Blink account diagnostics UI" not in javascript:
                javascript += _UI_PATCH_JS
                response.set_data(javascript)
                response.headers["Content-Length"] = str(len(response.get_data()))
            return response

        self.app.view_functions[app_js_endpoint] = patched_app_js

    WebServer.__init__ = patched_init
