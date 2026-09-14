"""Add helper templates to the MyBlink schedule form.

Templates only prefill the existing scheduler fields; users can still edit every
value before creating the rule. The rolling snooze presets deliberately use
Blink's standard four-hour notification snooze so they do not depend on Custom
Snooze subscription support.
"""

from __future__ import annotations

from web_server import WebServer


_UI_PATCH_JS = r"""

// MyBlink schedule helper templates.
(() => {
    const previousFetch = window.fetch.bind(window);

    const dayMap = {
        mon: 0,
        tue: 1,
        wed: 2,
        thu: 3,
        fri: 4,
        sat: 5,
        sun: 6,
    };

    const templates = {
        custom: {
            title: 'Custom schedule',
            description: 'Start with the normal schedule form and choose every value yourself.',
        },
        always_snooze: {
            title: 'Indefinitely — Always Snooze Notifications',
            description: 'Snoozes notifications for 4 hours and renews the snooze every 3 hours. Motion detection and recording stay enabled. This stays continuous while MyBlink is running.',
            action: 'camera_notification_snooze',
            intervalHours: 3,
            intervalMinutes: 0,
            durationHours: 4,
            nameSuffix: 'Always Snooze Notifications',
        },
        always_snooze_subscription: {
            title: 'Indefinitely — Custom Snooze (Subscription)',
            description: 'Uses Blink Custom Snooze for 24 hours and renews every 23 hours. Fewer API calls, but requires an eligible Blink subscription/trial that supports Custom Snooze.',
            action: 'camera_notification_snooze',
            intervalHours: 23,
            intervalMinutes: 0,
            durationHours: 24,
            nameSuffix: 'Always Snooze Notifications (24h)',
        },
        keep_motion_disabled: {
            title: 'Keep Motion Detection Disabled',
            description: 'Disables motion detection immediately and re-applies the disabled state every 24 hours. Use this when you do not want the camera to detect or record motion.',
            action: 'camera_motion_disable',
            intervalHours: 24,
            intervalMinutes: 0,
            durationHours: null,
            nameSuffix: 'Keep Motion Disabled',
        },
        keep_motion_enabled: {
            title: 'Keep Motion Detection Enabled',
            description: 'Enables motion detection immediately and re-applies the enabled state every 24 hours.',
            action: 'camera_motion_enable',
            intervalHours: 24,
            intervalMinutes: 0,
            durationHours: null,
            nameSuffix: 'Keep Motion Enabled',
        },
        hourly_thumbnail: {
            title: 'Refresh Thumbnail Hourly',
            description: 'Captures a fresh thumbnail once every hour.',
            action: 'camera_thumbnail',
            intervalHours: 1,
            intervalMinutes: 0,
            durationHours: null,
            nameSuffix: 'Hourly Thumbnail',
        },
        daily_thumbnail: {
            title: 'Refresh Thumbnail Daily',
            description: 'Captures a fresh thumbnail once every 24 hours.',
            action: 'camera_thumbnail',
            intervalHours: 24,
            intervalMinutes: 0,
            durationHours: null,
            nameSuffix: 'Daily Thumbnail',
        },
    };

    function setValue(id, value) {
        const element = document.getElementById(id);
        if (!element) return;
        element.value = value == null ? '' : String(value);
        element.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function clearRestrictions() {
        setValue('scheduleStartMinute', '');
        setValue('scheduleStartTime', '');
        setValue('scheduleEndTime', '');
        document.querySelectorAll('#scheduleForm input[type="checkbox"]').forEach(cb => {
            cb.checked = false;
        });
    }

    function selectedTargetName() {
        const value = document.getElementById('scheduleTarget')?.value || '';
        const separator = value.indexOf(':');
        return separator >= 0 ? value.slice(separator + 1) : '';
    }

    function renderTemplateHelp(templateKey) {
        const box = document.getElementById('scheduleTemplateHelp');
        if (!box) return;
        const template = templates[templateKey] || templates.custom;
        box.innerHTML = `
            <strong>${template.title}</strong>
            <div style="margin-top: 0.35rem; color: var(--text-secondary); line-height: 1.45;">
                ${template.description}
            </div>
        `;
    }

    function applyTemplate(templateKey) {
        const template = templates[templateKey] || templates.custom;
        renderTemplateHelp(templateKey);
        if (templateKey === 'custom') return;

        setValue('scheduleAction', template.action);
        setValue('scheduleIntervalHours', template.intervalHours);
        setValue('scheduleIntervalMinutes', template.intervalMinutes);
        setValue('scheduleDuration', template.durationHours);
        clearRestrictions();

        const targetName = selectedTargetName();
        const name = targetName
            ? `${targetName} — ${template.nameSuffix}`
            : template.nameSuffix;
        setValue('scheduleName', name);
    }

    function installTemplatePicker() {
        const form = document.getElementById('scheduleForm');
        if (!form || document.getElementById('scheduleTemplate')) return;

        const grid = form.querySelector('.settings-grid');
        if (!grid) return;

        const wrapper = document.createElement('div');
        wrapper.id = 'scheduleTemplateSection';
        wrapper.style.marginBottom = '1rem';
        wrapper.innerHTML = `
            <div class="form-group" style="margin-bottom: 0.75rem;">
                <label class="form-label">Template</label>
                <select id="scheduleTemplate" class="form-select">
                    <option value="custom">Custom schedule</option>
                    <option value="always_snooze">Indefinitely — Always Snooze Notifications</option>
                    <option value="always_snooze_subscription">Indefinitely — Custom Snooze (Subscription)</option>
                    <option value="keep_motion_disabled">Keep Motion Detection Disabled</option>
                    <option value="keep_motion_enabled">Keep Motion Detection Enabled</option>
                    <option value="hourly_thumbnail">Refresh Thumbnail Hourly</option>
                    <option value="daily_thumbnail">Refresh Thumbnail Daily</option>
                </select>
                <small style="color: var(--text-secondary);">Templates prefill the form below. You can change any field before creating the schedule.</small>
            </div>
            <div id="scheduleTemplateHelp" style="padding: 0.8rem 1rem; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-secondary); font-size: 0.875rem;"></div>
        `;
        form.insertBefore(wrapper, grid);

        const select = document.getElementById('scheduleTemplate');
        select?.addEventListener('change', event => applyTemplate(event.target.value));

        document.getElementById('scheduleTarget')?.addEventListener('change', () => {
            const selected = document.getElementById('scheduleTemplate')?.value || 'custom';
            if (selected !== 'custom') {
                const template = templates[selected];
                const targetName = selectedTargetName();
                if (template && targetName) {
                    setValue('scheduleName', `${targetName} — ${template.nameSuffix}`);
                }
            }
        });

        renderTemplateHelp('custom');
    }

    // The original form still serializes weekday names (mon/tue/...). Normalize
    // them here before the notification-snooze compatibility layer handles the
    // rest of the schedule payload.
    window.fetch = async function(input, init = {}) {
        const url = typeof input === 'string' ? input : (input && input.url) || '';
        const method = String(init.method || 'GET').toUpperCase();
        let effectiveInit = init;

        if (url === '/api/schedules' && method === 'POST' && init.body) {
            try {
                const payload = JSON.parse(init.body);
                if (typeof payload.days_of_week === 'string') {
                    payload.days_of_week = payload.days_of_week
                        .split(',')
                        .map(value => value.trim().toLowerCase())
                        .map(value => Object.prototype.hasOwnProperty.call(dayMap, value) ? dayMap[value] : Number(value))
                        .filter(value => Number.isInteger(value) && value >= 0 && value <= 6);
                    if (payload.days_of_week.length === 0) payload.days_of_week = null;
                }
                effectiveInit = { ...init, body: JSON.stringify(payload) };
            } catch (error) {
                console.warn('Could not normalize schedule weekdays:', error);
            }
        }

        return previousFetch(input, effectiveInit);
    };

    if (typeof MyBlinkApp !== 'undefined') {
        const previousRenderSchedulesPage = MyBlinkApp.prototype.renderSchedulesPage;
        if (previousRenderSchedulesPage) {
            MyBlinkApp.prototype.renderSchedulesPage = function(...args) {
                const result = previousRenderSchedulesPage.apply(this, args);
                setTimeout(installTemplatePicker, 0);
                return result;
            };
        }
    }

    const observer = new MutationObserver(() => {
        if (document.getElementById('scheduleForm') && !document.getElementById('scheduleTemplate')) {
            installTemplatePicker();
        }
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
})();
"""


def _find_app_js_endpoint(app) -> str | None:
    for rule in app.url_map.iter_rules():
        if rule.rule == "/static/app.js" and "GET" in rule.methods:
            return rule.endpoint
    return None


def apply_schedule_templates_ui() -> None:
    """Append schedule helper templates to app.js responses."""
    original_init = WebServer.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)

        endpoint = _find_app_js_endpoint(self.app)
        if not endpoint:
            self.logger.warning("app.js route not found; schedule template UI patch skipped")
            return

        original_view = self.app.view_functions[endpoint]

        def patched_app_js(*view_args, **view_kwargs):
            response = self.app.make_response(original_view(*view_args, **view_kwargs))
            response.direct_passthrough = False
            javascript = response.get_data(as_text=True)
            if "MyBlink schedule helper templates" not in javascript:
                javascript += _UI_PATCH_JS
                response.set_data(javascript)
                response.headers["Content-Length"] = str(len(response.get_data()))
            return response

        self.app.view_functions[endpoint] = patched_app_js

    WebServer.__init__ = patched_init
