"""Root-level compatibility alias for ``import nightshift``."""

from pydicom.nightshift import *  # noqa: F403
from pydicom.nightshift import __all__  # noqa: F401


def main() -> int:
    from pydicom.nightshift.__main__ import main as nightshift_main

    return nightshift_main()


if __name__ == "__main__":
    raise SystemExit(main())
