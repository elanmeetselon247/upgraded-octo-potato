"""Allow ``python3 -m gituses ...``."""

from .cli import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
