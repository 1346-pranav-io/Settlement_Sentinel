"""
Vercel serverless entry point for Settlement Sentinel.

Vercel runs this file in isolation, so we add the project root to sys.path
so that `from app.xxx import ...` (absolute) imports resolve correctly.
Static files are served by Vercel's built-in CDN (configured in vercel.json)
rather than FastAPI's StaticFiles mount.
"""
import sys
import os

# Make `app` importable as a package from the project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.main import app  # noqa: E402  (import after sys.path patch)
