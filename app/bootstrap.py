"""MyBlink bootstrap that applies local compatibility fixes."""

import myblink
from compatibility_fixes import apply_runtime_fixes
from http_debug_bootstrap import apply_http_debug_tracing
from rate_limit_body_fix import apply_rate_limit_body_fix
from scheduling_guards import apply_scheduling_guards


apply_runtime_fixes(myblink)
apply_rate_limit_body_fix()
apply_scheduling_guards()
apply_http_debug_tracing()


if __name__ == "__main__":
    myblink.main()
