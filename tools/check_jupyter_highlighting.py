"""Browser smoke for an installed Jupyter wheel (requires playwright and Edge).

Run in an isolated environment containing the wheel, jupyterlab and notebook:
  python tools/check_jupyter_highlighting.py
Pass --channel chromium after `python -m playwright install chromium` on Linux.
No BSL code is executed and no 1C installation is needed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen

from playwright.sync_api import expect, sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default="msedge")
    parser.add_argument("--baseline", action="store_true", help="Only collect frontend errors, with the extension disabled")
    parser.add_argument("--output", type=Path, default=Path("artifacts/jupyter-highlighting"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="onec-highlighting-") as temporary:
        root = Path(temporary)
        source = '%%bsl\nЕсли Истина Тогда\n    Сообщить("Привет, мир!"); // комментарий\nКонецЕсли;'
        notebook = {
            "nbformat": 4, "nbformat_minor": 5,
            "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"}},
            "cells": [{"id": "bsl-smoke", "cell_type": "code", "metadata": {},
                       "execution_count": None, "outputs": [], "source": source}],
        }
        (root / "highlighting.ipynb").write_text(json.dumps(notebook), encoding="utf-8")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        token = secrets.token_hex(24)
        base = f"http://127.0.0.1:{port}"
        env = {**os.environ, "JUPYTER_CONFIG_DIR": str(root / "config")}
        with (args.output / "server.log").open("w", encoding="utf-8") as log:
            server = subprocess.Popen(
                [sys.executable, "-m", "notebook", "--no-browser",
                 f"--ServerApp.root_dir={root}", f"--ServerApp.port={port}",
                 "--ServerApp.port_retries=0", "--ServerApp.ip=127.0.0.1",
                 f"--IdentityProvider.token={token}"], env=env, stdout=log, stderr=log,
            )
            try:
                for _ in range(150):
                    if server.poll() is not None:
                        raise RuntimeError("Jupyter exited; see server.log")
                    try:
                        with urlopen(f"{base}/api/status?token={token}", timeout=1):
                            break
                    except OSError:
                        time.sleep(0.2)
                else:
                    raise RuntimeError("Jupyter startup timed out; see server.log")
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(channel=args.channel, headless=True)
                    for frontend, route in [("lab", "lab/tree"), ("notebook", "notebooks")]:
                        page = browser.new_page(viewport={"width": 1280, "height": 800})
                        errors: list[str] = []
                        page.on("pageerror", lambda error: errors.append(error.stack or str(error)))
                        page.goto(f"{base}/{route}/highlighting.ipynb?token={token}")
                        editor = page.locator(".jp-CodeCell .cm-content").first
                        expect(editor).to_be_visible(timeout=60000)
                        if args.baseline:
                            page.wait_for_timeout(3000)
                            print(f"BASELINE {frontend}: {errors}")
                            page.close()
                            continue
                        keyword = editor.locator("span").filter(has_text="Если").first
                        expect(keyword).to_be_visible(timeout=30000)
                        assert editor.inner_text() == source
                        page.screenshot(path=str(args.output / f"{frontend}.png"))

                        # Real editor input must switch back and forth without a reload.
                        editor.click()
                        editor.press("ControlOrMeta+a")
                        page.keyboard.insert_text("def example():\n    return 1")
                        expect(editor.locator("span").filter(has_text="def").first).to_be_visible()
                        editor.press("ControlOrMeta+a")
                        replacement = '%%bsl\nIf True Then\n    Message("Hello");\nEndIf;'
                        page.keyboard.insert_text(replacement)
                        expect(editor.locator("span").filter(has_text="EndIf").first).to_be_visible()
                        assert editor.inner_text() == replacement
                        # These Notebook core errors also reproduce with our
                        # extension disabled (--baseline), in 7.1.1 / 7.6.2.
                        # Preserve them as evidence; reject any new errors.
                        known_notebook_errors = (
                            "Error: Command 'filebrowser:open-path' not registered.",
                            "TypeError: Cannot read properties of undefined (reading 'schema')",
                        )
                        unexpected = [error for error in errors if not (
                            frontend == "notebook"
                            and error.startswith(known_notebook_errors)
                            and "/static/notebook/" in error
                        )]
                        (args.output / f"{frontend}-errors.json").write_text(
                            json.dumps(errors, indent=2), encoding="utf-8"
                        )
                        assert not unexpected, unexpected
                        print(f"PASS {frontend}: saved BSL, Python restore, English BSL, source preserved")
                        page.close()
                    browser.close()
            finally:
                try:
                    request = Request(f"{base}/api/shutdown", data=b"", method="POST",
                                      headers={"Authorization": f"token {token}"})
                    with urlopen(request, timeout=5):
                        pass
                except OSError:
                    server.terminate()
                try:
                    server.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


if __name__ == "__main__":
    main()
