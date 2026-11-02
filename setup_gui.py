"""Compatibility launcher for the unified browser panel (no Tkinter required)."""
import asyncio

from server import run_panel


if __name__ == "__main__":
    asyncio.run(run_panel())
