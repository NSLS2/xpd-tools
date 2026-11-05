"""Launch the XPD-D data-acquisition GUI: ``python -m xpd_tools.gui``."""

from xpd_tools.gui.window import run

XPDD_SPEC = "xpd_tools.xpdd_config:XPDD_GUI"


def main(argv: list[str] | None = None):
    """Launch the GUI with XPD-D's spec."""
    run(XPDD_SPEC, argv)


if __name__ == "__main__":
    main()
