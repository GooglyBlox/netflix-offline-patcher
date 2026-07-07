"""Remove the Netflix Games SDK login gate from a Netflix game so it runs offline.

The login check lives in the Netflix SDK, not the game, and every title bundles the same
SDK. On Android the SDK is smali in classes.dex; on iOS it is a native framework reached
over a C ABI. Each platform has its own package (android/, ios/) organised by SDK
generation and engine, so new generations and frameworks slot in without touching the rest.
"""
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
REPO_ROOT = PKG_DIR.parent
MARKER = "netflix-offline-patcher"
