"""Existing smoke-test entry point, updated for seven-pollutant monitoring.

Requires Playwright and Chromium. Set DASHBOARD_BASE_URL for a running server.
"""
from pathlib import Path
import runpy

if __name__ == '__main__':
    runpy.run_path(str(Path(__file__).with_name('hardware_browser_smoke.py')), run_name='__main__')
