"""MyBlink bootstrap that applies local compatibility fixes."""

import myblink
from blink_account_diagnostics import apply_blink_account_diagnostics
from compatibility_fixes import apply_runtime_fixes
from configure_passthrough_fix import apply_configure_passthrough_fix
from http_debug_bootstrap import apply_http_debug_tracing
from notification_snooze_fix import apply_notification_snooze_fix
from notification_snooze_ui_safe import apply_notification_snooze_ui_safe
from rate_limit_body_fix import apply_rate_limit_body_fix
from schedule_templates_ui import apply_schedule_templates_ui
from scheduling_guards import apply_scheduling_guards
from web_rate_limit_ui import apply_web_rate_limit_ui


apply_runtime_fixes(myblink)
apply_rate_limit_body_fix()
apply_scheduling_guards()
apply_http_debug_tracing()
apply_notification_snooze_fix()
apply_web_rate_limit_ui()
apply_configure_passthrough_fix()
apply_notification_snooze_ui_safe()
apply_schedule_templates_ui()
apply_blink_account_diagnostics()


if __name__ == "__main__":
    myblink.main()
