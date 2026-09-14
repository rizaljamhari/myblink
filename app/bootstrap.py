"""MyBlink bootstrap that applies local compatibility fixes."""

import myblink
from compatibility_fixes import apply_runtime_fixes
from configure_passthrough_fix import apply_configure_passthrough_fix
from http_debug_bootstrap import apply_http_debug_tracing
from notification_snooze_fix import apply_notification_snooze_fix
from notification_snooze_ui import apply_notification_snooze_ui
from rate_limit_body_fix import apply_rate_limit_body_fix
from scheduling_guards import apply_scheduling_guards
from web_rate_limit_ui import apply_web_rate_limit_ui


apply_runtime_fixes(myblink)
apply_rate_limit_body_fix()
apply_scheduling_guards()
apply_http_debug_tracing()
apply_notification_snooze_fix()
apply_web_rate_limit_ui()
apply_configure_passthrough_fix()
apply_notification_snooze_ui()


if __name__ == "__main__":
    myblink.main()
