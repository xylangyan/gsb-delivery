"""Root-level compatibility entry point."""

from solution import *  # noqa: F403
from solution import main, solve, run  # noqa: F401


if __name__ == "__main__":
    raise SystemExit(main())
