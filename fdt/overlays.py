from .overlay import *  # noqa: F401,F403
from .overlay import (  # noqa: F401
    compose_overlays,
    merge_overlays,
    compose_dtbo,
    merge_dtbo,
    apply_overlay,
    apply_overlays,
    OverlayComposer,
    ComposedOverlay,
    OverlayComposition,
    CompositionReport,
    SourceReference,
    OverlayError,
    OverlayComposeError,
    OverlayTargetNotFound,
    OverlayFixupError,
    FixupOutOfRangeError,
)


def __getattr__(name):
    aliases = {
        'compose': compose_overlays,
        'merge': merge_overlays,
        'build_composite_overlay': compose_overlays,
    }
    if name in aliases:
        return aliases[name]
    raise AttributeError(name)


def main():
    from fdt.__main__ import main as cli_main
    cli_main()


if __name__ == '__main__':
    main()
