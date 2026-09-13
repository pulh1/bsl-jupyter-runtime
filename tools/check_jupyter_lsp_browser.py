"""Check BSL foreign documents, completion and diagnostics in real JupyterLab."""

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
from urllib.error import HTTPError

from playwright.sync_api import expect, sync_playwright


class DiagnosticWindow:
    """Acceptance-only routing of real diagnostics around a transient oracle."""
    MAX_MESSAGES = 64
    MAX_BYTES = 2 * 1024 * 1024

    def __init__(self):
        self.versions, self.latest, self.queue = {}, {}, []
        self.queued_bytes = 0
        self.active, self.target, self.selected = False, None, None
        self.failure = None

    @property
    def queued_count(self):
        return len(self.queue)

    def client(self, socket, message):
        if message.get('method') in ('textDocument/didOpen', 'textDocument/didChange'):
            document = message['params']['textDocument']
            self.versions[socket, document['uri']] = document.get('version')

    def _diagnostic(self, raw):
        assert type(raw) is str, 'diagnostic-message-invalid'
        message = json.loads(raw)
        assert isinstance(message, dict), 'diagnostic-message-invalid'
        if message.get('method') != 'textDocument/publishDiagnostics':
            return None
        params = message.get('params', {})
        assert (type(params.get('uri')) is str and type(params.get('version')) is int
                and params['version'] >= 0 and isinstance(params.get('diagnostics'), list)
                and all(isinstance(item, dict) for item in params['diagnostics'])), 'diagnostic-message-invalid'
        return params

    def _remember(self, socket, raw, params):
        key = socket, params['uri']
        self.latest.pop(key, None)
        self.latest[key] = raw
        # Retaining a delivered anchor is also bounded, independently of the
        # isolation queue. Eviction only releases memory; no wire message drops.
        while (len(self.latest) > self.MAX_MESSAGES
               or sum(len(item.encode('utf-8')) for item in self.latest.values()) > self.MAX_BYTES):
            self.latest.pop(next(iter(self.latest)))

    def server(self, socket, raw):
        params = self._diagnostic(raw)
        if params is None:
            return False
        if self.active:
            size = len(raw.encode('utf-8'))
            assert (self.queued_count < self.MAX_MESSAGES and self.queued_bytes + size <= self.MAX_BYTES), 'diagnostic-queue-limit'
            self.queue.append((socket, raw, size))
            self.queued_bytes += size
            return True
        self._remember(socket, raw, params)
        return False

    def begin(self, socket, uri):
        assert not self.active and not self.queue, 'diagnostic-window-already-active'
        version = self.versions.get((socket, uri))
        assert type(version) is int and version >= 0, 'diagnostic-current-version-unavailable'
        self.target, self.selected, self.active = (socket, uri, version), None, True
        self.check()

    def begin_held(self, held):
        diagnostics = [(socket, raw) for kind, socket, raw in held if kind == 'diagnostics']
        assert len(diagnostics) == 1, 'held-diagnostic-identity-unavailable'
        socket, raw = diagnostics[0]
        params = self._diagnostic(raw)
        assert params is not None, 'held-diagnostic-identity-invalid'
        self.begin(socket, params['uri'])

    def check(self):
        if self.failure:
            raise AssertionError(self.failure)
        if self.target:
            socket, uri, version = self.target
            assert self.versions.get((socket, uri)) == version, 'document-version-changed'

    def anchor(self):
        self.check()
        if self.selected is not None:
            return self.selected
        socket, uri, version = self.target
        raw = self.latest.get((socket, uri))
        params = self._diagnostic(raw) if raw else None
        if params and params['version'] == version and params['diagnostics']:
            self.selected = params  # Already delivered: do not replay it again.
            return params
        for index, (owner, raw, size) in enumerate(self.queue):
            params = self._diagnostic(raw)
            if owner is socket and params['uri'] == uri and params['version'] == version and params['diagnostics']:
                self.queue.pop(index)
                self.queued_bytes -= size
                socket.send(raw)  # Intentional anchor replay precedes held stale messages.
                self._remember(socket, raw, params)
                self.selected = params
                return params
        return None

    def finish(self):
        self.active = False
        failures = []
        while self.queue:
            socket, raw, size = self.queue.pop(0)
            self.queued_bytes -= size
            try:
                socket.send(raw)
                self._remember(socket, raw, self._diagnostic(raw))
            except Exception as error:
                failures.append(type(error).__name__)
        self.target, self.selected = None, None
        assert not failures, 'diagnostic-drain-delivery-failed'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=Path("tests/fixtures/onec/JupyterBslTestFixture"))
    parser.add_argument("--channel", default="msedge")
    parser.add_argument("--output", type=Path, default=Path("artifacts/lsp-browser"))
    parser.add_argument('--runtime-acceptance', action='store_true', help='Additional lifecycle/isolation acceptance with a test runtime')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="onec-lsp-notebooks-") as temporary, \
            tempfile.TemporaryDirectory(prefix="onec-lsp-current-sources-") as source_temporary:
        root = Path(temporary)
        from check_jupyter_bsl_lsp import fingerprints
        from check_jupyter_lsp_runtime import copy_source_fixture
        input_tree = fingerprints(args.source_root.resolve())
        source_root = copy_source_fixture(args.source_root.resolve(), Path(source_temporary))
        if args.runtime_acceptance:
            from check_jupyter_bsl_lsp import create_edt_fixture
            create_edt_fixture(root, 'NotebookOnlyLeak')
        startup = f'''import sys
sys.path.extend({[str(Path(p).resolve()) for p in ('tests/unit', 'packages/mcp/src')]!r})
import onec_runtime, onec_runtime_jupyter
assert 'site-packages' in onec_runtime.__file__ and 'site-packages' in onec_runtime_jupyter.__file__, 'Not testing the installed wheel'
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from onec_runtime.session import RuntimeSession
from onec_runtime_jupyter.extension import install_runtime
from test_extension_session import session_config, _Closeable, _IdleRdbg
from test_runtime_api import _semantic_snapshot_runtime, _common_module_catalog, _SemanticSnapshotFailureTarget, _worker_module_source_unit
_lsp_fixture = TemporaryDirectory(prefix="onec-lsp-runtime-fixture-")
_lsp_work = Path(_lsp_fixture.name)
_lsp_config = replace(session_config(_lsp_work), source_root=Path({str(source_root)!r}))
_lsp_target = _SemanticSnapshotFailureTarget()
_lsp_api = _semantic_snapshot_runtime(_lsp_work, _common_module_catalog("JupyterBslFixtureCalleeServer"), target=_lsp_target)
_lsp_runtime = RuntimeSession(_lsp_config, _Closeable(), _Closeable(), _IdleRdbg(), _lsp_api, SimpleNamespace())
install_runtime(get_ipython(), _lsp_runtime)
'''
        sources = [startup, "%%bsl\nНачальноеЗначение = 1;", "%%bsl\nРезультат = JupyterBslFixtureCalleeServer."]
        notebook = {"nbformat": 4, "nbformat_minor": 5, "metadata": {
            "kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"}},
            "cells": [{"id": f"bsl-{i}", "cell_type": "code", "metadata": {},
                       "execution_count": None, "outputs": [], "source": source}
                      for i, source in enumerate(sources)]}
        (root / "lsp.ipynb").write_text(json.dumps(notebook), encoding="utf-8")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        token = secrets.token_hex(24)
        env = {**os.environ, "JUPYTER_CONFIG_DIR": str(root / "config"),
               "ONEC_BSL_LANGUAGE_SERVER": str(args.server.resolve())}
        env.pop('PYTHONPATH', None)
        env.pop('ONEC_BSL_SOURCE_ROOT', None)
        # Startup logs include token URLs; evidence contains only bounded summaries.
        with open(os.devnull, 'w') as log:
            server = subprocess.Popen([sys.executable, "-m", "jupyterlab", "--no-browser",
                f"--ServerApp.root_dir={root}", f"--ServerApp.port={port}",
                "--ServerApp.port_retries=0", "--ServerApp.ip=127.0.0.1",
                "--LabApp.expose_app_in_browser=True", f"--IdentityProvider.token={token}"],
                env=env, cwd=root, stdout=log, stderr=log)
            frames = []
            errors = []
            http_statuses = []
            pin_requests = []
            context_replies = []
            fallback_requests = []
            try:
                for _ in range(150):
                    try:
                        with urlopen(f"{base}/api/status?token={token}", timeout=1):
                            break
                    except OSError:
                        if server.poll() is not None:
                            raise RuntimeError("Jupyter exited during startup")
                        time.sleep(0.2)
                source_path = "CommonModules/JupyterBslFixtureCalleeServer/Ext/Module.bsl"
                source_file = source_root / source_path
                if not source_file.is_file():
                    selected = source_root / 'src' if (source_root / 'src/CommonModules').is_dir() else source_root
                    source_path = 'CommonModules/JupyterBslFixtureCalleeServer/Module.bsl'
                    source_file = selected / source_path
                source_before = source_file.read_bytes()
                endpoint = base + "/onec-bsl/sources/"
                headers = {"Authorization": f"token {token}"}
                for path, method in [("Configuration.xml", "GET"),
                                     ("%2e%2e/outside.bsl", "GET"), (source_path, "PUT")]:
                    try:
                        with urlopen(Request(endpoint + path, headers=headers, method=method), timeout=5):
                            raise AssertionError(f"Unexpectedly allowed {method} {path}")
                    except HTTPError as error:
                        assert error.code == (405 if method == "PUT" else 404), error
                assert source_file.read_bytes() == source_before
                print("PASS: source endpoint rejects writes, traversal and non-BSL files", flush=True)
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(channel=args.channel, headless=True)
                    page = browser.new_page(viewport={"width": 1400, "height": 900})
                    if not args.runtime_acceptance:
                        page.clock.install()
                    def page_error(error):
                        # Exact tested-host bug: pending StaticNotebook RAF uses
                        # an observer cleared by onBeforeDetach. Record openly;
                        # other versions/locations/messages remain failures.
                        known = (str(error) == "Cannot read properties of null (reading 'observe')"
                            and 'jlab_core.a3196067513a13d4.js' in error.stack
                            and ':13616:35728' in error.stack)
                        errors.append('jupyterlab-4.6.3-detached-observer' if known else 'unexpected-page-error')
                    page.on('pageerror', page_error)
                    page.on('response', lambda response: http_statuses.append(response.status)
                            if '/onec-bsl/contexts' in response.url else None)
                    def context_response(response):
                        if '/onec-bsl/contexts' in response.url and response.status in (200, 201):
                            context_replies.append(response.json())
                    page.on('response', context_response)
                    page.on('response', lambda response: fallback_requests.append((response.request.method, response.status))
                            if '/api/contents/.lsp_symlink/' in response.url else None)
                    page.on('response', lambda response: pin_requests.append((response.request.method, response.status))
                            if '/onec-bsl/sources/' in response.url and '/pins/' in response.url else None)
                    held = []
                    hold = {'diagnostics': False, 'completion': False, 'definition': False}
                    methods = {}
                    transition = {'enabled': False, 'versions': [], 'diagnostics': []}
                    diagnostic_window = DiagnosticWindow()
                    def route_socket(client_socket):
                        server_socket = client_socket.connect_to_server()
                        def from_client(raw):
                            message = json.loads(raw)
                            diagnostic_window.client(client_socket, message)
                            if 'id' in message:
                                uri = message.get('params', {}).get('textDocument', {}).get('uri')
                                methods[client_socket, message['id']] = (message.get('method'), uri,
                                    diagnostic_window.versions.get((client_socket, uri)))
                            if transition['enabled'] and message.get('method') == 'textDocument/didChange':
                                transition['versions'].append(message['params']['textDocument']['version'])
                            server_socket.send(raw)
                        def from_server(raw):
                            message = json.loads(raw)
                            requested = methods.get((client_socket, message.get('id')), (None, None, None))[0]
                            kind = ('diagnostics' if message.get('method') == 'textDocument/publishDiagnostics'
                                    and message.get('params', {}).get('diagnostics') else
                                    'completion' if requested == 'textDocument/completion' else
                                    'definition' if requested == 'textDocument/definition' else None)
                            if transition['enabled'] and kind == 'diagnostics':
                                transition['diagnostics'].append((client_socket, raw, message['params']['version']))
                            elif kind and hold[kind]:
                                held.append((kind, client_socket, raw))
                                hold[kind] = False
                            else:
                                try:
                                    if diagnostic_window.server(client_socket, raw):
                                        return
                                except (AssertionError, ValueError, TypeError):
                                    # Do not silently drop an overflow/malformed wire
                                    # message. Forward it, but this oracle cannot pass.
                                    diagnostic_window.failure = 'diagnostic-routing-failed'
                                client_socket.send(raw)
                        client_socket.on_message(from_client)
                        server_socket.on_message(from_server)
                    page.route_web_socket('**/lsp/ws/onec-bsl*', route_socket)

                    def socket_opened(ws):
                        if "/lsp/ws/" in ws.url:
                            ws.on("framesent", lambda payload: frames.append({"direction": "client", "message": json.loads(payload)}))
                            ws.on("framereceived", lambda payload: frames.append({"direction": "server", "message": json.loads(payload)}))

                    page.on("websocket", socket_opened)
                    page.goto(f"{base}/lab/tree/lsp.ipynb?token={token}")
                    editors = page.locator(".jp-CodeCell .cm-content")
                    expect(editors).to_have_count(3, timeout=60000)
                    page.wait_for_timeout(4000)
                    status = page.locator('.onec-bsl-runtime-status')
                    expect(status).to_contain_text('virtual only', timeout=30000)
                    assert 'index convergence' not in status.inner_text()
                    if page.locator('.jp-Dialog').count():
                        raise AssertionError('Startup dialog: ' + page.locator('.jp-Dialog').inner_text())
                    expect(page.locator('.jp-Dialog')).to_have_count(0)
                    if args.runtime_acceptance:
                        from check_jupyter_lsp_runtime import exercise_browser_runtime
                        evidence = exercise_browser_runtime(page, root, startup, sources, source_root,
                                                            observed_frames=frames, observed_contexts=context_replies)
                        assert 'unexpected-page-error' not in errors, errors
                        evidence['known_host_page_errors'] = len(errors)
                        (args.output / 'runtime-summary.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
                        print(f'PASS runtime acceptance; disclosed host detached-observer errors: {len(errors)}', flush=True)
                        browser.close()
                        return
                    # Native hover must survive movement/drag through the unmapped
                    # magic header, before any project runtime has been installed.
                    editors.nth(1).click(); editors.nth(1).press('ControlOrMeta+a')
                    page.keyboard.insert_text('%%bsl\nПроцедура NotebookLocalAlpha()\nКонецПроцедуры\n')
                    editors.nth(2).click(); editors.nth(2).press('ControlOrMeta+a')
                    page.keyboard.insert_text('%%bsl\nNotebookLocalAlpha();')
                    page.wait_for_timeout(1500)
                    def native_hover():
                        point = page.evaluate('window.jupyterapp.shell.currentWidget.content.widgets[2].editor.getCoordinateForPosition({line:1,column:8})')
                        page.mouse.move(5, 5)
                        page.keyboard.down('Control')
                        try:
                            page.mouse.move(point['left'] + 2, (point['top'] + point['bottom']) / 2)
                            expect(page.locator('.lsp-hover')).to_contain_text('NotebookLocalAlpha', timeout=15000)
                        finally:
                            page.keyboard.up('Control')
                            page.mouse.move(5, 5)
                    native_hover()
                    points = page.evaluate('''() => {
                      const editor = window.jupyterapp.shell.currentWidget.content.widgets[2].editor;
                      return [editor.getCoordinateForPosition({line:0,column:0}), editor.getCoordinateForPosition({line:1,column:8})];
                    }''')
                    page.mouse.move(points[0]['left'] + 2, (points[0]['top'] + points[0]['bottom']) / 2)
                    page.wait_for_timeout(700)
                    native_hover()
                    page.mouse.move(points[0]['left'] + 1, (points[0]['top'] + points[0]['bottom']) / 2)
                    page.mouse.down()
                    page.mouse.move(points[1]['left'], (points[1]['top'] + points[1]['bottom']) / 2, steps=10)
                    page.mouse.up()
                    assert page.evaluate('''() => {
                      const selection=window.jupyterapp.shell.currentWidget.content.widgets[2].editor.getSelection();
                      return selection.start.line !== selection.end.line;
                    }'''), 'Magic-header drag failed to select across lines'
                    native_hover()
                    for index in (1, 2):
                        editors.nth(index).click(); editors.nth(index).press('ControlOrMeta+a')
                        page.keyboard.insert_text(sources[index])
                    print('PASS: rootless native hover survives magic-header movement and multiline drag selection', flush=True)
                    # Use the notebook startup cell with a real ipykernel and explicit
                    # RuntimeSession fixture; no 1C process or registry injection.
                    editors.nth(0).click()
                    page.evaluate("window.jupyterapp.commands.execute('notebook:run-cell')")
                    try:
                        expect(status).to_contain_text('project', timeout=30000)
                    except AssertionError:
                        print('Context HTTP statuses:', http_statuses, flush=True)
                        print('Startup error count:', page.locator('.jp-OutputArea-error').count(), flush=True)
                        print('Startup execution:', page.evaluate("(() => { const cell=window.jupyterapp.shell.currentWidget.content.model.cells.get(0); return {count:cell.executionCount, outputs:cell.outputs.toJSON().map(o=>({type:o.output_type,error:o.ename}))}; })()"), flush=True)
                        raise
                    expect(status).to_contain_text('ready', timeout=30000)
                    expect(status).to_contain_text('index convergence unconfirmed')
                    print('PASS: real kernel startup automatically binds runtime project and child readiness', flush=True)
                    editor = editors.nth(2)
                    editor.click()
                    editor.press("ControlOrMeta+End")
                    # Indexing is asynchronous; retry the actual completion command.
                    completion = page.locator(".jp-Completer-item").filter(has_text="ВыполнитьШаг").first
                    for _ in range(5):
                        editor.click()
                        editor.press("ControlOrMeta+End")
                        page.evaluate("window.jupyterapp.commands.execute('completer:invoke-notebook')")
                        try:
                            expect(completion).to_be_visible(timeout=3000)
                            break
                        except AssertionError:
                            editor.press("Escape")
                    expect(completion).to_be_visible()
                    print("PASS: project method completion in the second %%bsl cell", flush=True)
                    editor.press("Escape")
                    editor.press("ControlOrMeta+a")
                    page.keyboard.insert_text("%%bsl\nЕсли Тогда\n")
                    marker = page.locator(".jp-CodeCell").nth(2).locator(".cm-lintRange-error")
                    expect(marker.first).to_be_visible(timeout=30000)
                    opens = [item["message"]["params"]["textDocument"] for item in frames
                             if item["message"].get("method") == "textDocument/didOpen"]
                    assert any("НачальноеЗначение" in item["text"] and "JupyterBslFixtureCalleeServer" in item["text"]
                               and "%%bsl" not in item["text"] for item in opens), 'BSL document extraction failed'
                    print("PASS: cells share a magic-free virtual document; diagnostics map to cell 2", flush=True)
                    editor.click()
                    editor.press("ControlOrMeta+a")
                    definition_source = "%%bsl\nРезультат = JupyterBslFixtureCalleeServer.ВыполнитьШаг(1);"
                    page.keyboard.insert_text(definition_source)
                    cleared_at = time.monotonic()
                    expect(marker).to_have_count(0, timeout=10000)
                    clear_latency_ms = int((time.monotonic() - cleared_at) * 1000)
                    # Place the cursor inside the method name, then use the standard LSP command.
                    editor.press("ControlOrMeta+End")
                    editor.press("ArrowLeft", no_wait_after=True)
                    for _ in range(5):
                        editor.press("ArrowLeft")
                    page.wait_for_timeout(1000)
                    page.evaluate("window.jupyterapp.commands.execute('lsp:jump-to-definition')")
                    target = page.locator(".jp-FileEditor .cm-content")
                    expect(target).to_be_visible(timeout=20000)
                    expect(target).to_contain_text("Функция ВыполнитьШаг")
                    assert page.evaluate("window.jupyterapp.shell.currentWidget.context.contentsModel.writable") is False
                    assert page.evaluate("window.jupyterapp.shell.currentWidget.content.editor.editor.state.readOnly") is True
                    expect(target).to_have_attribute('contenteditable', 'false')
                    expect(page.locator('.onec-bsl-source-status')).to_contain_text('current file')
                    page.evaluate('window.__onecSourceWidget = window.jupyterapp.shell.currentWidget; void 0')
                    source_widget_id = page.evaluate('window.__onecSourceWidget.id')
                    source_widget = page.locator(f'[id="{source_widget_id}"]')
                    target = source_widget.locator('.cm-content')
                    source_status = source_widget.locator('.onec-bsl-source-status')
                    def reopen_source():
                        page.evaluate("async () => { await window.jupyterapp.commands.execute('docmanager:open', {path:window.__onecSourceWidget.context.path}); }")
                    print("PASS: standard go-to-definition opens project source outside Jupyter root, read-only", flush=True)
                    # Preserve existing CRLF without Windows text I/O doubling CR.
                    changed_source = source_before.decode('utf-8-sig') + '\nФункция ИзменениеПослеПросмотра() Экспорт\nВозврат 2;\nКонецФункции\n'
                    source_file.write_text(changed_source, encoding='utf-8', newline='')
                    assert source_file.read_bytes() == changed_source.encode('utf-8'), 'Owned fixture write changed intended bytes'
                    reopen_source()
                    expect(target).to_contain_text('ИзменениеПослеПросмотра', timeout=15000)
                    assert page.evaluate('window.jupyterapp.shell.currentWidget === window.__onecSourceWidget')
                    expect(source_status).to_contain_text('BSL: current file')
                    saved_source = source_file.read_bytes()
                    # Capture an actual definition response while the file exists.
                    # Deliver after deletion so the real jumper calls openOrReveal;
                    # docmanager:open's separate metadata GET would reject first.
                    page.evaluate("async () => { await window.jupyterapp.commands.execute('docmanager:open', {path:'lsp.ipynb'}); }")
                    editor.click(); editor.press('ControlOrMeta+End')
                    for _ in range(6): editor.press('ArrowLeft')
                    hold['definition'] = True
                    page.evaluate("() => { void window.jupyterapp.commands.execute('lsp:jump-to-definition'); }")
                    for _ in range(150):
                        if any(kind == 'definition' for kind, _, _ in held): break
                        page.wait_for_timeout(100)
                    definition_reply = next(item for item in held if item[0] == 'definition')
                    held.remove(definition_reply)
                    assert 'error' not in json.loads(definition_reply[2]), 'Held definition was not a successful reply'
                    page.evaluate('''() => {
                      const watch = window.__onecFallbackWatch = {editable:false, unavailable:false};
                      const sample = () => {
                        watch.editable ||= !!document.querySelector('.jp-FileEditor .cm-content[contenteditable="true"]');
                        watch.unavailable ||= [...document.querySelectorAll('.onec-bsl-source-status')].some(node => node.textContent.includes('source unavailable'));
                      };
                      watch.observer = new MutationObserver(sample);
                      watch.observer.observe(document.body, {subtree:true, childList:true, attributes:true});
                    }''')
                    source_file.unlink()
                    definition_reply[1].send(definition_reply[2])
                    page.wait_for_function("window.__onecSourceWidget.node.querySelector('.onec-bsl-source-status').textContent.includes('displayed content is not current')")
                    for _ in range(150):
                        if ('GET', 404) in fallback_requests: break
                        page.wait_for_timeout(100)
                    assert ('GET', 404) in fallback_requests, 'Reserved fallback route was not exercised'
                    page.wait_for_timeout(500)
                    observed_fallback = page.evaluate('''() => {
                      const watch = window.__onecFallbackWatch; watch.observer.disconnect();
                      return {editable:watch.editable, unavailable:watch.unavailable};
                    }''')
                    assert observed_fallback == {'editable':False, 'unavailable':True}, observed_fallback
                    # Public IContext.revert also reports its failed load through Jupyter's normal dialog.
                    while page.locator('.jp-Dialog').count():
                        expect(page.locator('.jp-Dialog')).to_contain_text('File Load Error')
                        page.locator('.jp-Dialog').get_by_role('button', name='Close', exact=True).click()
                        page.wait_for_timeout(200)
                    source_file.write_bytes(saved_source)
                    assert source_file.read_bytes() == saved_source, 'Restoration changed saved source bytes'
                    reopen_source()
                    expect(source_status).to_contain_text('BSL: current file', timeout=15000)
                    expect(target).to_contain_text('ИзменениеПослеПросмотра')
                    assert not pin_requests, 'Current-file viewer requested retained-source leases'
                    print('PASS: repeat opening the active viewer refreshes disk bytes; deletion is visibly unavailable; restoration recovers', flush=True)
                    page.evaluate("async () => { await window.jupyterapp.commands.execute('docmanager:open', {path: 'lsp.ipynb'}); }")
                    editor.click()
                    editor.press("ControlOrMeta+a")
                    page.keyboard.insert_text(sources[2])
                    source_messages = [item['message']['params']['textDocument']['uri'] for item in frames
                        if item['message'].get('method') in ('textDocument/didOpen', 'textDocument/didChange')]
                    assert source_file.as_uri() not in source_messages, 'Viewer opened a project overlay'
                    changed_completion = page.locator('.jp-Completer-item').filter(has_text='ИзменениеПослеПросмотра').first
                    for _ in range(5):
                        # A scan begun by the deliberate deletion/restoration can
                        # legitimately fence one query; re-invoke after current readiness.
                        expect(status).to_contain_text('ready', timeout=10000)
                        editor.click(); editor.press('ControlOrMeta+End')
                        page.wait_for_timeout(1000)
                        page.evaluate("window.jupyterapp.commands.execute('completer:invoke-notebook')")
                        try:
                            expect(completion).to_be_visible(timeout=3000)
                            expect(changed_completion).to_be_visible(timeout=3000)
                            break
                        except AssertionError:
                            editor.press('Escape')
                    expect(completion).to_be_visible()
                    expect(changed_completion).to_be_visible()
                    print("PASS: notebook completion survives opening the source document", flush=True)
                    editor.press('Escape')
                    # Hold real gateway messages after the server sent them but before
                    # upstream frontend delivery. No fabricated diagnostics or replies.
                    hold['diagnostics'] = True
                    editor.press('ControlOrMeta+a'); page.keyboard.insert_text('%%bsl\nЕсли Тогда\n')
                    for _ in range(100):
                        if any(kind == 'diagnostics' for kind, _, _ in held): break
                        page.wait_for_timeout(100)
                    assert any(kind == 'diagnostics' for kind, _, _ in held), 'No real diagnostic captured'
                    editor.press('ControlOrMeta+a'); page.keyboard.insert_text(sources[2])
                    page.wait_for_timeout(800)
                    hold['completion'] = True
                    page.evaluate("window.jupyterapp.commands.execute('completer:invoke-notebook')")
                    for _ in range(100):
                        if any(kind == 'completion' for kind, _, _ in held): break
                        page.wait_for_timeout(100)
                    assert any(kind == 'completion' for kind, _, _ in held), 'No real completion captured'
                    bindings_before = http_statuses.count(201)
                    previous_status = context_replies[-1]
                    reinstall_code = ('_lsp_target = _SemanticSnapshotFailureTarget()\n'
                        '_lsp_api = _semantic_snapshot_runtime(_lsp_work, _common_module_catalog("JupyterBslFixtureCalleeServer"), target=_lsp_target)\n'
                        '_lsp_runtime = RuntimeSession(_lsp_config, _Closeable(), _Closeable(), _IdleRdbg(), _lsp_api, SimpleNamespace())\n'
                        'install_runtime(get_ipython(), _lsp_runtime)')
                    page.evaluate('''async code => {
                      const kernel = window.jupyterapp.shell.currentWidget.sessionContext.session.kernel;
                      const future = kernel.requestExecute({code, store_history: false});
                      const reply = await future.done;
                      if (reply.content.status !== 'ok') throw new Error('Fixture reinstall failed');
                    }''', reinstall_code)
                    for _ in range(300):
                        if any(reply['binding_id'] == previous_status['binding_id'] and reply['epoch'] > previous_status['epoch']
                               and reply['analysis_state'] == 'ready' for reply in context_replies): break
                        page.wait_for_timeout(100)
                    assert any(reply['binding_id'] == previous_status['binding_id'] and reply['epoch'] > previous_status['epoch']
                               and reply['analysis_state'] == 'ready' for reply in context_replies), 'No acknowledged replacement'
                    expect(status).to_contain_text('ready', timeout=30000)
                    expect(marker).to_have_count(0, timeout=10000)
                    try:
                        diagnostic_window.begin_held(held)
                        target_socket, target_uri, target_version = diagnostic_window.target
                        assert sorted(kind for kind, _, _ in held) == ['completion', 'diagnostics'], 'Expected the two original held stale responses'
                        for kind, socket_route, raw in held:
                            message = json.loads(raw)
                            if kind == 'diagnostics':
                                old_uri, old_version = message['params']['uri'], message['params']['version']
                            else:
                                _, old_uri, old_version = methods[socket_route, message['id']]
                            assert (socket_route is target_socket and old_uri == target_uri
                                    and type(old_version) is int and old_version < target_version), 'Held response is not from the prior socket/document version'
                        anchor_deadline = time.monotonic() + 10
                        anchor = diagnostic_window.anchor()
                        while anchor is None and time.monotonic() < anchor_deadline:
                            page.wait_for_timeout(25)
                            anchor = diagnostic_window.anchor()
                        assert anchor is not None, 'Current diagnostic anchor unavailable'
                        current_rows = [item for item in anchor['diagnostics'] if item.get('severity', 4) < 4]
                        assert sorted(item.get('code') for item in current_rows) == [
                            'SemicolonPresence', 'UnusedLocalVariable', 'UnusedLocalVariable'], 'Unexpected current fixture diagnostics'
                        assert all(type(item.get('message')) is str and item['message'] for item in current_rows), 'Malformed current fixture diagnostics'
                        page.evaluate("void window.jupyterapp.commands.execute('lsp:show-diagnostics-panel')")
                        remaining = anchor_deadline - time.monotonic()
                        assert remaining > 0, 'Current diagnostic anchor/render deadline'
                        page.wait_for_function('''messages => {
                          const panel = document.querySelector('.lsp-diagnostics-listing');
                          const rows = panel ? [...panel.querySelectorAll('tbody tr')].map(row => row.textContent || '') : [];
                          return rows.length === messages.length && messages.every(message => rows.some(row => row.includes(message)));
                        }''', arg=[item['message'] for item in current_rows], timeout=remaining * 1000)
                        diagnostic_window.check()
                        page.evaluate('''() => {
                          const cell = document.querySelectorAll('.jp-CodeCell')[2];
                          const panel = document.querySelector('.lsp-diagnostics-listing');
                          if (!panel) throw new Error('Diagnostics panel unavailable');
                          const rows = () => JSON.stringify([...panel.querySelectorAll('tbody tr')].map(row => row.textContent).sort());
                          const baseline = rows();
                          const watch = window.__onecDiagnosticWatch = {maximum: 0, frames: 0, running: true, panelChanged: false};
                          const sample = () => {
                            watch.maximum = Math.max(watch.maximum, cell.querySelectorAll('.cm-lintRange-error').length);
                            watch.panelChanged ||= rows() !== baseline;
                          };
                          watch.observer = new MutationObserver(sample);
                          watch.observer.observe(cell, {subtree:true, childList:true, attributes:true});
                          watch.observer.observe(panel, {subtree:true, childList:true, characterData:true});
                          const frame = () => {sample(); watch.frames++; if(watch.running) requestAnimationFrame(frame);};
                          requestAnimationFrame(frame);
                        }''')
                        fence_time = time.monotonic()
                        try:
                            # Deliberately replay the anchor before these old real
                            # replies; unrelated real diagnostics remain queued.
                            for _, socket_route, raw in held: socket_route.send(raw)
                            held.clear()
                            page.wait_for_timeout(1200)
                        finally:
                            observed = page.evaluate('''() => {
                              const watch = window.__onecDiagnosticWatch; watch.running = false; watch.observer.disconnect();
                              return {maximum:watch.maximum, frames:watch.frames, panelChanged:watch.panelChanged};
                            }''')
                        diagnostic_window.check()
                        assert observed['frames'] > 0 and observed['maximum'] == 0, 'Transient stale diagnostic resurrection'
                        assert not observed['panelChanged'], 'Delayed diagnostics changed the current panel database'
                        expect(completion).not_to_be_visible()
                        expect(marker).to_have_count(0)
                        assert http_statuses.count(201) == bindings_before, 'Reinstall recreated notebook association'
                    finally:
                        # Every remaining actual diagnostic is attempted once in
                        # queue order, even if an assertion fails. Global original
                        # order is intentionally changed by anchor/stale replay.
                        diagnostic_window.finish()
                    assert diagnostic_window.queued_count == diagnostic_window.queued_bytes == 0
                    (args.output / 'diagnostic-window.json').write_text(json.dumps({
                        'status': 'PASS', 'anchor_version': anchor['version'],
                        'same_socket_document_identity': True, 'document_version_stable': True,
                        'transient': observed, 'remaining_queued_messages': 0, 'remaining_queued_bytes': 0,
                        'limits': {'messages': 64, 'wire_bytes': 2 * 1024 * 1024, 'anchor_render_seconds': 10},
                        'delivery_order': 'Actual current anchor, original held stale responses, then all remaining real queued diagnostics in queue order; deliberately not global original order',
                    }, indent=2), encoding='utf-8')
                    page.evaluate("window.jupyterapp.commands.execute('completer:invoke-notebook')")
                    expect(completion).to_be_visible(timeout=15000)
                    print('PASS: delayed real diagnostics/completion rejected after installation replacement; clear check within '
                          f'{int((time.monotonic() - fence_time) * 1000)} ms, same binding', flush=True)
                    editor.press('Escape')
                    page.evaluate("window.jupyterapp.commands.execute('lsp:show-diagnostics-panel')")
                    page.evaluate("async () => { await window.jupyterapp.commands.execute('docmanager:open', {path:'lsp.ipynb'}); }")
                    def execute_fixture(code):
                        page.evaluate('''async code => {
                          const kernel = window.jupyterapp.shell.currentWidget.sessionContext.session.kernel;
                          const reply = await kernel.requestExecute({code, store_history:false}).done;
                          if (reply.content.status !== 'ok') throw new Error('Fixture lifecycle action failed: ' + reply.content.ename + ': ' + reply.content.evalue);
                        }''', code)
                    before_close = context_replies[-1]
                    close_poll = len(context_replies)
                    execute_fixture('_lsp_runtime.close()')
                    for _ in range(100):
                        if len(context_replies) > close_poll: break
                        page.wait_for_timeout(100)
                    assert len(context_replies) > close_poll and context_replies[-1] == before_close, 'Runtime close changed static project authority'
                    # Remove only the owned temporary project's pathname, then restore
                    # it even on failure. Confirm an acknowledged virtual fallback
                    # releases queries while its project-unavailable reason stays visible.
                    assert source_root.parent == Path(source_temporary)
                    unavailable_root = source_root.with_name('project-unavailable')
                    source_root.rename(unavailable_root)
                    try:
                        expect(status).to_contain_text('virtual only', timeout=30000)
                        expect(status).to_contain_text('unavailable')
                        fallback_status = context_replies[-1]
                        # Windows can first lose the watched directory notification
                        # handle; both exact reasons acknowledge the virtual child.
                        assert fallback_status['analysis_state'] == 'unavailable'
                        assert fallback_status['analysis_reason'] in ('workspace-unavailable', 'workspace-notification-unavailable')
                        assert (fallback_status['binding_id'], fallback_status['epoch']) == (before_close['binding_id'], before_close['epoch'])
                        expect(status).to_contain_text(fallback_status['analysis_reason'])
                        editor.click(); editor.press('ControlOrMeta+a')
                        page.keyboard.insert_text('%%bsl\nСообщ')
                        page.wait_for_timeout(1000)
                        page.evaluate("window.jupyterapp.commands.execute('completer:invoke-notebook')")
                        expect(page.locator('.jp-Completer-item').filter(has_text='Сообщить').first).to_be_visible(timeout=15000)
                        editor.press('Escape')
                    finally:
                        unavailable_root.rename(source_root)
                    before_none = context_replies[-1]
                    execute_fixture(reinstall_code.replace('RuntimeSession(_lsp_config,', 'RuntimeSession(replace(_lsp_config, source_root=None),'))
                    for _ in range(300):
                        if any(reply['epoch'] > before_none['epoch'] and reply['analysis_state'] == 'ready'
                               and reply['mode'] == 'virtual-only' for reply in context_replies): break
                        page.wait_for_timeout(100)
                    assert any(reply['epoch'] > before_none['epoch'] and reply['analysis_state'] == 'ready'
                               and reply['mode'] == 'virtual-only' for reply in context_replies), 'None reinstall did not acknowledge rootless authority'
                    expect(status).to_contain_text('virtual only')
                    assert 'index convergence' not in status.inner_text()
                    execute_fixture(reinstall_code)
                    expect(status).to_contain_text('project', timeout=30000)
                    expect(status).to_contain_text('ready', timeout=30000)
                    editor.click(); editor.press('ControlOrMeta+a'); page.keyboard.insert_text(sources[2])
                    page.wait_for_timeout(1000)
                    print('PASS: runtime close preserves project; confirmed project fallback serves built-ins; None reinstall clears and explicit reinstall restores root', flush=True)
                    editor.click(); editor.press('ControlOrMeta+End')
                    hold['completion'] = True
                    page.evaluate("window.jupyterapp.commands.execute('completer:invoke-notebook')")
                    for _ in range(100):
                        if any(kind == 'completion' for kind, _, _ in held): break
                        page.wait_for_timeout(100)
                    assert held, 'No completion available to delay across kernel restart'
                    page.evaluate('''async () => { await window.jupyterapp.shell.currentWidget.sessionContext.session.kernel.restart(); }''')
                    expect(status).to_contain_text('virtual only', timeout=30000)
                    for _, socket_route, raw in held: socket_route.send(raw)
                    held.clear(); page.wait_for_timeout(1000)
                    expect(completion).not_to_be_visible()
                    expect(status).to_contain_text('ready', timeout=30000)
                    editor.click(); editor.press('ControlOrMeta+a')
                    page.keyboard.insert_text('%%bsl\nЕсли Тогда\n')
                    expect(marker.first).to_be_visible(timeout=15000)
                    # Keep the actual old kernel and association alive long enough
                    # for the transition-generated didChange to be processed by its
                    # old child. Only HTTP delivery is held; no protocol is fabricated.
                    pending_http = []
                    holding_http = [True]
                    def hold_association(route):
                        if holding_http[0] and route.request.method in ('POST', 'DELETE'): pending_http.append(route)
                        else: route.continue_()
                    def hold_restart(route):
                        if holding_http[0]: pending_http.append(route)
                        else: route.continue_()
                    page.route('**/onec-bsl/contexts**', hold_association)
                    page.route('**/api/kernels/*/restart', hold_restart)
                    transition['enabled'] = True
                    page.evaluate('''() => {
                      window.__onecRestart = window.jupyterapp.shell.currentWidget.sessionContext.session.kernel.restart();
                    }''')
                    for _ in range(150):
                        if any(version in transition['versions'] for _, _, version in transition['diagnostics']): break
                        page.wait_for_timeout(100)
                    old_diagnostic = next((item for item in transition['diagnostics'] if item[2] in transition['versions']), None)
                    assert old_diagnostic is not None and pending_http, 'No old-child transition-version diagnostic captured'
                    transition['diagnostics'].remove(old_diagnostic)
                    holding_http[0] = False
                    for route in pending_http: route.continue_()
                    page.unroute('**/onec-bsl/contexts**', hold_association)
                    page.unroute('**/api/kernels/*/restart', hold_restart)
                    page.evaluate('async () => { await window.__onecRestart; }')
                    expect(status).to_contain_text('ready', timeout=30000)
                    for _ in range(150):
                        if any(version > old_diagnostic[2] for _, _, version in transition['diagnostics']): break
                        page.wait_for_timeout(100)
                    assert any(version > old_diagnostic[2] for _, _, version in transition['diagnostics']), 'No acknowledged-authority version advance'
                    expect(marker).to_have_count(0, timeout=10000)
                    page.evaluate('''() => {
                      const cell = document.querySelectorAll('.jp-CodeCell')[2];
                      const panel = document.querySelector('.lsp-diagnostics-listing');
                      const rows = () => JSON.stringify([...panel.querySelectorAll('tbody tr')].map(row => row.textContent).sort());
                      const baseline = rows();
                      const watch = window.__onecTransitionWatch = {maximum:0, changed:false, running:true, frames:0};
                      const sample = () => {watch.maximum = Math.max(watch.maximum, cell.querySelectorAll('.cm-lintRange-error').length); watch.changed ||= rows() !== baseline;};
                      watch.observer = new MutationObserver(sample);
                      watch.observer.observe(cell, {subtree:true, childList:true, attributes:true});
                      watch.observer.observe(panel, {subtree:true, childList:true, characterData:true});
                      const frame = () => {sample(); watch.frames++; if(watch.running) requestAnimationFrame(frame);}; requestAnimationFrame(frame);
                    }''')
                    old_diagnostic[0].send(old_diagnostic[1])
                    page.wait_for_timeout(1200)
                    watch = page.evaluate('''() => {
                      const watch = window.__onecTransitionWatch; watch.running=false; watch.observer.disconnect();
                      return {maximum:watch.maximum, changed:watch.changed, frames:watch.frames};
                    }''')
                    assert watch['frames'] > 0 and watch['maximum'] == 0 and not watch['changed'], 'Transition-version diagnostic resurrected after replacement'
                    transition['enabled'] = False
                    newest = max(transition['diagnostics'], key=lambda item: item[2])
                    newest[0].send(newest[1])
                    expect(marker.first).to_be_visible(timeout=15000)
                    print('PASS: old-child transition-version diagnostic rejected after acknowledged replacement; newest diagnostics still render', flush=True)
                    assert page.evaluate('window.__onecSourceWidget.content.editor.editor.state.readOnly') is True
                    page.evaluate('window.__onecSourceWidget.close()')
                    assert not pin_requests, 'Current viewer made retained-source requests'
                    if page.locator('.jp-Dialog').count():
                        page.locator('.jp-Dialog').get_by_role('button', name='OK', exact=True).click()
                    expect(page.locator('.jp-Dialog')).to_have_count(0)
                    before_reconnect = context_replies[-1]['binding_id']
                    page.reload()
                    expect(editors).to_have_count(3, timeout=60000)
                    expect(status).to_contain_text('ready', timeout=30000)
                    expect(status).to_contain_text('virtual only')
                    assert 'index convergence' not in status.inner_text()
                    current = context_replies[-1]
                    assert current['binding_id'] != before_reconnect
                    claims = [frame['message']['params']['settings']['onecProjectBinding'] for frame in frames
                        if frame['direction'] == 'client' and frame['message'].get('method') == 'workspace/didChangeConfiguration'
                        and 'onecProjectBinding' in frame['message'].get('params', {}).get('settings', {})]
                    assert any(claim['binding_id'] == current['binding_id'] for claim in claims), 'Ready browser binding has no actual socket claim'
                    assert all(set(claim) == {'document_uri', 'binding_id'} for claim in claims)
                    editors.nth(2).click(); editors.nth(2).press('ControlOrMeta+a')
                    page.keyboard.insert_text('%%bsl\nСообщ')
                    page.wait_for_timeout(1000)
                    page.evaluate("window.jupyterapp.commands.execute('completer:invoke-notebook')")
                    expect(page.locator('.jp-Completer-item').filter(has_text='Сообщить').first).to_be_visible(timeout=15000)
                    print(f'PASS: reconnect claims the fresh acknowledged binding and serves built-ins; no pins; restart rejects delayed completion; native clear observed in {clear_latency_ms} ms', flush=True)
                    assert 'unexpected-page-error' not in errors, errors
                    print(f'Disclosed host detached-observer errors: {len(errors)}', flush=True)
                    browser.close()
            finally:
                counts = {}
                for frame in frames:
                    method = frame['message'].get('method', 'response')
                    counts[method] = counts.get(method, 0) + 1
                (args.output / "lsp-summary.json").write_text(json.dumps(counts, indent=2), encoding="utf-8")
                (args.output / "browser-errors.json").write_text(json.dumps(errors), encoding="utf-8")
                try:
                    with urlopen(Request(f"{base}/api/shutdown", data=b"", method="POST",
                        headers={"Authorization": f"token {token}"}), timeout=5):
                        pass
                except OSError:
                    server.terminate()
                try:
                    server.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
                assert fingerprints(args.source_root.resolve()) == input_tree, 'read-only input source tree changed'


if __name__ == "__main__":
    main()
