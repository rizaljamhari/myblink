"""MyBlink bootstrap that applies local compatibility fixes."""

import myblink
from compatibility_fixes import apply_runtime_fixes
from scheduling_guards import apply_scheduling_guards


apply_runtime_fixes(myblink)
apply_scheduling_guards()


if __name__ == "__main__":
    myblink.main()
