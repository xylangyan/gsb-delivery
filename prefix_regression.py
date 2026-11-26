from kneed.prefix_regression import *  # noqa: F401,F403
from kneed.prefix_regression import (
    BatchSegment,
    PrefixRegression,
    archive_to_xy,
    compare_prefixes,
    evaluate_prefix,
    expand_segments,
    find_disagreements,
    find_first_difference,
    find_first_disagreement,
    main,
    plateau_certificate,
    prefix_regression,
    regress_prefix_range,
    regression_entry,
    run_regression,
)

if __name__ == "__main__":
    raise SystemExit(main())
