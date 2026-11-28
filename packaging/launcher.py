"""Entry point for the frozen executables and for start-up entries when running from source."""
import os
import sys

if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vrp.app import main  # noqa: E402

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()          # the live-transcript worker process in a frozen build (1.1.0)
    sys.exit(main())
