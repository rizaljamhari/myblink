"""Safe frontend patch for notification snooze semantics.

This replaces the earlier document-wide MutationObserver implementation. UI
changes are applied only from the relevant MyBlink render hooks, preventing DOM
mutation feedback loops that can freeze the browser.
"""

from __future__ import annotations

from web_server import WebServer


_UI_PATCH_JS = r"""

// MyBlink notification-snooze semantic fixes (safe version).
(() => {
    const nativeFetch = window.fetch.bind(window);

    function scheduleLabel(rule) {
        const targetType = rule.target_type || '';
        const action = rule.action || '';
        if (targetType === 'camera' && action === 'snooze') return 'Snooze Notifications';
        if (targetType === 'camera' && action === 'motion_disable') return 'Disable Motion Detection';
        if (targetType === 'camera' && action === 'arm') return 'Enable Motion Detection';
        if (targetType === 'camera' && action === 'thumbnail') return 'Capture Thumbnail';
        if (targetType === 'sync' && action === 'snooze') return 'Disarm Sync Module';
        if (targetType === 'sync' && action === 'arm') return 'Arm Sync Module';
        return action || 'Schedule';
    }

    function adaptRuleForLegacyUI(rule) {
        const result = { ...rule };
        result.target_id = result.target_id || result.target_name || '';
        result.rule_name = result.rule_name || `${result.target_id} – ${scheduleLabel(result)}`;

        if (result.target_type === 'camera') {
            if (result.action === 'snooze') {
                result.rule_type = 'CAMERA_NOTIFICATION_SNOOZE';
                result.action = 'snooze';
            } else if (result.action === 'motion_disable') {
                result.rule_type = 'CAMERA_MOTION';
                result.action = 'disable';
            } else if (result.action === 'arm') {
                result.rule_type = 'CAMERA_MOTION';
                result.action = 'enable';
            } else if (result.action === 'thumbnail') {
                result.rule_type = 'CAMERA_THUMBNAIL';
                result.action = 'capture';
            }
        } else if (result.target_type === 'sync') {
            if (result.action === 'snooze') {
                result.rule_type = 'SYNC_DISARM';
                result.action = 'disarm';
            } else if (result.action === 'arm') {
                result.rule_type = 'SYNC_ARM';
                result.action = 'arm';
            }
        }
        return result;
    }

    function normalizeDays(value) {
        if (value == null || value === '') return null;
        if (Array.isArray(value)) return value.map(Number);
        return String(value)
            .split(',')
            .map(v => Number(v.trim()))
            .filter(v => Number.isInteger(v));
    }

    function transformScheduleCreatePayload(payload) {
        if (!payload || typeof payload !== 'object') return payload;

        const targetControl = document.getElementById('scheduleTarget');
        const actionControl = document.getElementById('scheduleAction');
        const selectedTarget = targetControl?.value || '';
        const selectedAction = actionControl?.value || '';
        const [uiTargetType, uiTargetName] = selectedTarget.split(':');

        const targetType = payload.target_type || uiTargetType;
        const targetName = payload.target_name || payload.target_id || uiTargetName;
        let action = payload.action;

        switch (selectedAction) {
            case 'camera_notification_snooze':
                action = 'snooze';
                break;
            case 'camera_motion_enable':
                action = 'arm';
                break;
            case 'camera_motion_disable':
                action = 'motion_disable';
                break;
            case 'camera_thumbnail':
                action = 'thumbnail';
                break;
            case 'sync_arm':
                action = 'arm';
                break;
            case 'sync_disarm':
                action = 'snooze';
                break;
            default:
                if (payload.rule_type === 'CAMERA_MOTION') {
                    action = payload.action === 'enable' ? 'arm' : 'motion_disable';
                } else if (payload.rule_type === 'CAMERA_THUMBNAIL') {
                    action = 'thumbnail';
                } else if (payload.rule_type === 'SYNC_ARM') {
                    action = 'arm';
                } else if (payload.rule_type === 'SYNC_DISARM') {
                    action = 'snooze';
                }
        }

        return {
            target_type: targetType,
            target_name: targetName,
            action,
            enabled: payload.enabled !== false,
            interval_hours: payload.interval_hours ?? null,
            interval_minutes: payload.interval_minutes ?? null,
            start_minute: payload.start_minute ?? null,
            duration_hours: payload.duration_hours ?? null,
            start_time: payload.start_time || null,
            end_time: payload.end_time || null,
            days_of_week: normalizeDays(payload.days_of_week),
        };
    }

    window.fetch = async function(input, init = {}) {
        const url = typeof input === 'string' ? input : (input && input.url) || '';
        const method = String(init.method || 'GET').toUpperCase();
        let effectiveInit = init;

        if (url === '/api/schedules' && method === 'POST' && init.body) {
            try {
                const originalPayload = JSON.parse(init.body);
                effectiveInit = {
                    ...init,
                    body: JSON.stringify(transformScheduleCreatePayload(originalPayload)),
                };
            } catch (error) {
                console.warn('Could not normalize schedule payload:', error);
            }
        }

        const response = await nativeFetch(input, effectiveInit);

        if (url === '/api/schedules' && method === 'GET' && response.ok) {
            try {
                const data = await response.clone().json();
                if (data && Array.isArray(data.rules)) {
                    data.rules = data.rules.map(adaptRuleForLegacyUI);
                    return new Response(JSON.stringify(data), {
                        status: response.status,
                        statusText: response.statusText,
                        headers: response.headers,
                    });
                }
            } catch (error) {
                console.warn('Could not adapt schedule response:', error);
            }
        }

        return response;
    };

    function patchScheduleOptions() {
        const select = document.getElementById('scheduleAction');
        if (!select) return;

        const motionEnable = select.querySelector('option[value="camera_motion_enable"]');
        const motionDisable = select.querySelector('option[value="camera_motion_disable"]');

        if (motionEnable && motionEnable.textContent !== 'Enable Motion Detection') {
            motionEnable.textContent = 'Enable Motion Detection';
        }
        if (motionDisable && motionDisable.textContent !== 'Disable Motion Detection') {
            motionDisable.textContent = 'Disable Motion Detection';
        }

        if (!select.querySelector('option[value="camera_notification_snooze"]')) {
            const option = document.createElement('option');
            option.value = 'camera_notification_snooze';
            option.textContent = 'Snooze Notifications';
            if (motionDisable && motionDisable.nextSibling) {
                select.insertBefore(option, motionDisable.nextSibling);
            } else {
                select.appendChild(option);
            }
        }
    }

    function replaceTextNode(element, oldText, newText) {
        element.childNodes.forEach(node => {
            if (node.nodeType === Node.TEXT_NODE && node.textContent.includes(oldText)) {
                node.textContent = node.textContent.replace(oldText, newText);
            }
        });
    }

    function patchCameraLabels(root = document) {
        root.querySelectorAll('.setting-label').forEach(element => {
            const text = element.textContent.trim();
            if (text === 'Snooze Motion') {
                replaceTextNode(element, 'Snooze Motion', 'Snooze Notifications');
            } else if (text === 'Arm Camera') {
                replaceTextNode(element, 'Arm Camera', 'Motion Detection');
            }
        });

        root.querySelectorAll('.setting-description').forEach(element => {
            const text = element.textContent.trim();
            if (text === 'Disable motion detection') {
                element.textContent = 'Pause motion notifications while keeping motion detection and recording enabled';
            } else if (text === 'Disable detection for 5 minutes') {
                element.textContent = 'Pause motion notifications for 5 minutes';
            } else if (text === 'Enable motion detection') {
                element.textContent = 'Enable or disable motion detection for this camera';
            }
        });

        root.querySelectorAll('.info-label').forEach(element => {
            if (element.textContent.trim() === 'Snoozed') {
                element.textContent = 'Notifications Snoozed';
            }
        });

        root.querySelectorAll('small').forEach(element => {
            if (element.textContent.includes('auto-unsnooze')) {
                element.textContent = 'Blink will automatically resume notifications after this duration.';
            }
        });
    }

    if (typeof MyBlinkApp !== 'undefined') {
        const originalFormatScheduleAction = MyBlinkApp.prototype.formatScheduleAction;
        MyBlinkApp.prototype.formatScheduleAction = function(schedule) {
            if (schedule?.rule_type === 'CAMERA_NOTIFICATION_SNOOZE') {
                const duration = Number(schedule.duration_hours || 0);
                return duration > 0
                    ? `Snooze Notifications (${duration}h)`
                    : 'Snooze Notifications';
            }
            return originalFormatScheduleAction.call(this, schedule);
        };

        const originalShowCameraModal = MyBlinkApp.prototype.showCameraModal;
        if (originalShowCameraModal) {
            MyBlinkApp.prototype.showCameraModal = function(...args) {
                const result = originalShowCameraModal.apply(this, args);
                setTimeout(() => patchCameraLabels(document), 0);
                return result;
            };
        }

        const originalRenderSchedulesPage = MyBlinkApp.prototype.renderSchedulesPage;
        if (originalRenderSchedulesPage) {
            MyBlinkApp.prototype.renderSchedulesPage = function(...args) {
                const result = originalRenderSchedulesPage.apply(this, args);
                setTimeout(() => patchScheduleOptions(), 0);
                return result;
            };
        }
    }

    // One-time initial pass only. No document-wide MutationObserver.
    document.addEventListener('DOMContentLoaded', () => {
        patchCameraLabels(document);
        patchScheduleOptions();
    });
})();
"""


def _find_app_js_endpoint(app) -> str | None:
    for rule in app.url_map.iter_rules():
        if rule.rule == "/static/app.js" and "GET" in rule.methods:
            return rule.endpoint
    return None


def apply_notification_snooze_ui_safe() -> None:
    """Append the safe notification-snooze frontend patch to app.js responses."""
    original_init = WebServer.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)

        endpoint = _find_app_js_endpoint(self.app)
        if not endpoint:
            self.logger.warning("app.js route not found; notification snooze UI patch skipped")
            return

        original_view = self.app.view_functions[endpoint]

        def patched_app_js(*view_args, **view_kwargs):
            response = self.app.make_response(original_view(*view_args, **view_kwargs))
            response.direct_passthrough = False
            javascript = response.get_data(as_text=True)
            if "MyBlink notification-snooze semantic fixes (safe version)" not in javascript:
                javascript += _UI_PATCH_JS
                response.set_data(javascript)
                response.headers["Content-Length"] = str(len(response.get_data()))
            return response

        self.app.view_functions[endpoint] = patched_app_js

    WebServer.__init__ = patched_init
