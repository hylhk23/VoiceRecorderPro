import sys

if __name__ == "__main__":
    # imported only here: the live-transcript worker process starts from this module too
    # (multiprocessing "spawn") and must not load the user interface
    from vrp.app import main
    sys.exit(main())
