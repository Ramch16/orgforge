"""Entry point PyInstaller bundles as the app's `vittics-builder-server` sidecar."""
import multiprocessing

from vittics_builder.desktop import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
