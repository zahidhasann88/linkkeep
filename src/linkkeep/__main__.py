"""Allow ``python -m linkkeep`` to behave exactly like the console script."""

from __future__ import annotations

from linkkeep.cli import main

if __name__ == "__main__":  # pragma: no cover - convenience entry point
    main()
