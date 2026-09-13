"""MyBlink bootstrap that applies local compatibility fixes."""

import myblink
from compatibility_fixes import apply_runtime_fixes


apply_runtime_fixes(myblink)


if __name__ == "__main__":
    myblink.main()
