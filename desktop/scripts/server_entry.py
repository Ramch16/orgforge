"""Entry point PyInstaller bundles as the app's `orgforge-server` sidecar."""
import multiprocessing

from orgforge.desktop import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
