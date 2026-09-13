"""Installed-wheel browser/kernel acceptance; the default backend is NOT live 1C.

Uses the public install_runtime path and a real RuntimeSession/RuntimeAPI with
explicit test transport/packer fixtures. No server registry is injected. Source,
protocol frames and credentials are never written to the evidence summaries.
"""
from __future__ import annotations

import argparse
from collections import deque
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlsplit

from playwright.sync_api import expect


def copy_source_fixture(source, owned_parent):
    """Copy only the configured tree, preserving LS-significant EDT src naming."""
    source, owned_parent = Path(source), Path(owned_parent)
    target = owned_parent / source.name
    shutil.copytree(source, target)
    return target


def execute(page, code):
    result = page.evaluate('''async code => {
      const kernel = window.jupyterapp.shell.currentWidget.sessionContext.session.kernel;
      const future = kernel.requestExecute({code, store_history:false});
      const reply = await future.done;
      return {status:reply.content.status, error:reply.content.ename};
    }''', code)
    assert result['status'] == 'ok', result


def edit(page, index, source):
    widget_id = page.evaluate('window.jupyterapp.shell.currentWidget.id')
    editor = page.locator(f'[id="{widget_id}"] .jp-CodeCell .cm-content:visible').nth(index)
    page.evaluate('''index => {
      const notebook=window.jupyterapp.shell.currentWidget.content;
      notebook.activeCellIndex=index;
      notebook.widgets[index].editor.focus();
    }''', index)
    editor.scroll_into_view_if_needed()
    editor.click()
    editor.press('ControlOrMeta+a')
    page.keyboard.insert_text(source)
    editor.press('ControlOrMeta+End')
    assert page.evaluate('''({index, source}) =>
      window.jupyterapp.shell.currentWidget.content.model.cells.get(index).sharedModel.getSource() === source
    ''', {'index':index, 'source':source}), 'Typed fixture must reach the intended notebook cell model'
    page.wait_for_timeout(700)
    return editor


def completion(page, source, expected, absent=None):
    editor = edit(page, 2, source)
    wanted = page.locator('.jp-Completer-item:visible').filter(has_text=expected).first
    for _ in range(8):
        page.evaluate("window.jupyterapp.commands.execute('completer:invoke-notebook')")
        try:
            expect(wanted).to_be_visible(timeout=2500)
            break
        except AssertionError:
            editor.press('Escape'); page.wait_for_timeout(500)
    expect(wanted).to_be_visible()
    if absent:
        expect(page.locator('.jp-Completer-item:visible').filter(has_text=absent)).to_have_count(0)
    editor.press('Escape')


def hover_failure_summary(frames, attempt_start):
    """Only bounded ordered request labels and response classes, never payloads."""
    labels, events = {}, deque(maxlen=64)
    for index, frame in enumerate(frames):
        message = frame['message']
        request_id = message.get('id')
        if type(request_id) not in (str, int):
            continue
        phase = 'before-attempt' if index < attempt_start else 'attempt'
        if frame['direction'] == 'client' and message.get('method') == 'textDocument/hover':
            if request_id not in labels and len(labels) < 32:
                labels[request_id] = len(labels)
            if request_id in labels:
                events.append({'kind': 'request', 'request': labels[request_id], 'phase': phase})
        elif frame['direction'] == 'server' and request_id in labels and 'method' not in message:
            event = {'kind': 'response', 'request': labels[request_id], 'phase': phase}
            if 'error' in message:
                code = message['error'].get('code')
                event.update(response='error', error_code=code if type(code) is int else None)
            else:
                event['response'] = ('missing' if 'result' not in message else 'null'
                    if message['result'] is None else 'nonempty' if message['result'] else 'empty')
            events.append(event)
    return list(events)


def hover(page, observed_frames, *, require_fresh=False):
    frame_start = len(observed_frames)
    edit(page, 2, '%%bsl\nNotebookLocalAlpha();')
    point = page.evaluate('window.jupyterapp.shell.currentWidget.content.widgets[2].editor.getCoordinateForPosition({line:1, column:5})')
    page.mouse.move(5, 5)
    page.keyboard.down('Control')
    try:
        page.mouse.move(point['left'] + 2, (point['top'] + point['bottom']) / 2)
        expect(page.locator('.lsp-hover')).to_contain_text('NotebookLocalAlpha', timeout=15000)
        if require_fresh:
            requests = {frame['message']['id'] for frame in observed_frames[frame_start:]
                        if frame['direction'] == 'client' and frame['message'].get('method') == 'textDocument/hover'}
            assert any(frame['direction'] == 'server' and frame['message'].get('id') in requests
                       and isinstance(frame['message'].get('result'), dict)
                       and frame['message']['result'].get('contents')
                       for frame in observed_frames[frame_start:]), 'Tooltip needs a new actual successful hover request'
    except AssertionError:
        print('HOVER FAILURE ' + json.dumps(hover_failure_summary(observed_frames, frame_start)), flush=True)
        raise
    finally:
        page.keyboard.up('Control')
        page.mouse.move(5, 5)


def no_notebook_indexing(page):
    editor = edit(page, 2, '%%bsl\nРезультат = NotebookOnlyLeak.')
    page.evaluate("window.jupyterapp.commands.execute('completer:invoke-notebook')")
    page.wait_for_timeout(1500)
    expect(page.locator('.jp-Completer-item:visible').filter(has_text='ДисковыйВызов')).to_have_count(0)
    editor.press('Escape')


def definition(page, method, arguments='1', *, return_to_notebook=True):
    editor = edit(page, 2, f'%%bsl\nРезультат = JupyterBslFixtureCalleeServer.{method}({arguments});')
    for _ in range(len(arguments) + 5): editor.press('ArrowLeft')
    page.evaluate("window.jupyterapp.commands.execute('lsp:jump-to-definition')")
    target = page.locator('.jp-FileEditor .cm-content:visible')
    expect(target).to_contain_text(f'Функция {method}', timeout=20000)
    assert page.evaluate('window.jupyterapp.shell.currentWidget.content.editor.editor.state.readOnly')
    assert page.evaluate('window.jupyterapp.shell.currentWidget.context.contentsModel.writable') is False
    page.evaluate('window.__onecCurrentSource = window.jupyterapp.shell.currentWidget; void 0')
    if return_to_notebook:
        page.evaluate("async () => { await window.jupyterapp.commands.execute('docmanager:open', {path:'lsp.ipynb'}); }")


def reload_code(method, revision, parameters='Первый', failure=None):
    source = f'Функция {method}({parameters}) Экспорт\nВозврат 1;\nКонецФункции\n'
    call = f'_lsp_runtime.load_worker_modules((_worker_module_source_unit("JupyterBslFixtureCalleeServer", {revision}, {source!r}),))'
    if failure:
        return (f'_lsp_previous = _lsp_runtime.runtime_api.worker_generation_handle\n_lsp_target.failure = {failure!r}\n'
            f'try:\n    {call}\nexcept Exception:\n    pass\nelse:\n    raise AssertionError("Expected confirmation failure")\n'
            + ('assert _lsp_runtime.runtime_api.worker_generation_handle is _lsp_previous\n' if failure != 'unknown' else '')
            + '_lsp_target.failure = None')
    return (f'_lsp_handle = {call}\nassert _lsp_runtime.runtime_api.worker_generation_handle is _lsp_handle\n'
            '_lsp_runtime.release_worker_generation(_lsp_handle)')


def exercise_browser_runtime(page, notebook_root, startup, sources, source_root, *, observed_frames, observed_contexts):
    from check_jupyter_bsl_lsp import SourceWriteOracle, fingerprints
    from onec_runtime_jupyter.lsp_contexts import normalize_source_root
    status = page.locator('.onec-bsl-runtime-status:visible')
    prefix = '%%bsl\nРезультат = JupyterBslFixtureCalleeServer.'
    source_files = fingerprints(source_root)
    oracle = SourceWriteOracle(source_root)
    normalized, reason = normalize_source_root(source_root)
    assert normalized and not reason
    module_dir = normalized / 'CommonModules/JupyterBslFixtureCalleeServer'
    module = module_dir / ('Ext/Module.bsl' if (module_dir / 'Ext').is_dir() else 'Module.bsl')
    original_module = module.read_bytes()
    def disk_source(method, parameters='Аргумент'):
        return f'Функция {method}({parameters}) Экспорт\r\nВозврат 1;\r\nКонецФункции\r\n'.encode('utf-8')
    oracle.save(module, disk_source('ДисковыйА'))
    assert not source_root.is_relative_to(notebook_root), 'definition fixture must be outside Jupyter root'
    notebook_sources = {p: sha256(p.read_bytes()).hexdigest() for p in notebook_root.rglob('*.bsl')}
    completion(page, '%%bsl\nСообщ', 'Сообщить')
    edit(page, 1, '%%bsl\nПроцедура NotebookLocalAlpha()\nКонецПроцедуры\n')
    completion(page, '%%bsl\nNotebookLoc', 'NotebookLocalAlpha')
    hover(page, observed_frames)
    root_point = page.evaluate('window.jupyterapp.shell.currentWidget.content.widgets[2].editor.getCoordinateForPosition({line:0,column:3})')
    page.mouse.move(root_point['left'] + 2, (root_point['top'] + root_point['bottom']) / 2)
    page.wait_for_timeout(700)
    hover(page, observed_frames)
    # Dragging through the disconnected magic header must still select text,
    # without poisoning the following native hover.
    points = page.evaluate('''() => {
      const editor=window.jupyterapp.shell.currentWidget.content.widgets[2].editor;
      return [editor.getCoordinateForPosition({line:0,column:0}),
        editor.getCoordinateForPosition({line:1,column:8})];
    }''')
    page.mouse.move(points[0]['left'] + 1, (points[0]['top'] + points[0]['bottom']) / 2)
    page.mouse.down()
    page.mouse.move(points[1]['left'], (points[1]['top'] + points[1]['bottom']) / 2, steps=10)
    page.mouse.up()
    assert page.evaluate('''() => {
      const selection=window.jupyterapp.shell.currentWidget.content.widgets[2].editor.getSelection();
      return selection.start.line !== selection.end.line;
    }'''), 'Disconnected-position guard must preserve multiline mouse selection'
    hover(page, observed_frames)
    no_notebook_indexing(page)
    edit(page, 2, '%%bsl\nЕсли Тогда\n')
    marker = page.locator('.jp-CodeCell').nth(2).locator('.cm-lintRange-error')
    expect(marker.first).to_be_visible(timeout=20000)
    print('PASS before startup: built-in/cross-cell completion, hover and mapped diagnostics', flush=True)

    rootless = startup.replace(f'source_root=Path({str(source_root.resolve())!r})', 'source_root=None')
    execute(page, rootless)
    expect(status).to_contain_text('virtual only', timeout=30000)
    completion(page, '%%bsl\nСообщ', 'Сообщить')
    hover(page, observed_frames, require_fresh=True)
    no_notebook_indexing(page)
    print('PASS source_root=None: real install_runtime remains virtual only', flush=True)

    execute(page, startup)
    expect(status).to_contain_text('project', timeout=30000)
    expect(status).to_contain_text('ready', timeout=30000)
    completion(page, prefix, 'ДисковыйА')
    execute(page, reload_code('ПринятыйА', 1))
    completion(page, prefix, 'ДисковыйА', 'ПринятыйА')
    original_bindings = {reply['binding_id'] for reply in observed_contexts}
    definition(page, 'ДисковыйА', return_to_notebook=False)
    source_widget_id = page.evaluate('window.__onecCurrentSource.id')
    source_widget = page.locator(f'[id="{source_widget_id}"]')
    source_context_path = page.evaluate('window.__onecCurrentSource.context.path')
    oracle.save(module, disk_source('ДисковыйБ', 'Первый, Второй'))
    page.evaluate('async () => { await window.__onecCurrentSource.context.revert(); }')
    expect(source_widget.locator('.cm-content')).to_contain_text('ДисковыйБ', timeout=20000)
    assert page.evaluate('window.jupyterapp.shell.currentWidget === window.__onecCurrentSource')
    expect(source_widget.locator('.onec-bsl-source-status')).to_contain_text('current file')
    saved_module = module.read_bytes()
    oracle.delete(module)
    page.evaluate('async () => { try { await window.__onecCurrentSource.context.revert(); } catch (_) {} }')
    expect(source_widget.locator('.onec-bsl-source-status')).to_contain_text('displayed content is not current', timeout=20000)
    assert page.evaluate('window.__onecCurrentSource.content.editor.editor.state.readOnly')
    while page.locator('.jp-Dialog').count():
        expect(page.locator('.jp-Dialog')).to_contain_text('File Load Error')
        page.get_by_role('button', name='Close', exact=True).click()
    oracle.save(module, saved_module)
    page.evaluate('async () => { await window.__onecCurrentSource.context.revert(); }')
    expect(source_widget.locator('.cm-content')).to_contain_text('ДисковыйБ', timeout=20000)
    page.evaluate('window.__onecCurrentSource.close()')
    page.evaluate("async () => { await window.jupyterapp.commands.execute('docmanager:open', {path:'lsp.ipynb'}); }")
    completion(page, prefix, 'ДисковыйБ', 'ПринятыйА')
    execute(page, reload_code('ПринятыйБ', 2, 'Первый, Второй'))
    completion(page, prefix, 'ДисковыйБ', 'ПринятыйБ')
    execute(page, reload_code('НеПринятый', 3, failure='swap_guard'))
    completion(page, prefix, 'ДисковыйБ', 'НеПринятый')
    definition(page, 'ДисковыйБ', '1, 2')
    assert page.evaluate("window.__onecCurrentSource.content.model.sharedModel.getSource().includes('ДисковыйБ(Первый, Второй)')")
    page.evaluate('window.__onecCurrentSource.close()')
    viewer_aliases = [frame['message']['params']['textDocument']['uri'] for frame in observed_frames
                     if frame['direction'] == 'client' and frame['message'].get('method') == 'textDocument/didOpen'
                     and unquote(urlsplit(frame['message']['params']['textDocument']['uri']).path).endswith(source_context_path)]
    assert viewer_aliases and all(urlsplit(uri).scheme == 'file' and not uri.startswith('onec-bsl:') for uri in viewer_aliases)
    assert {reply['binding_id'] for reply in observed_contexts} == original_bindings, 'Source viewer created project authority'
    expect(page.locator('.jp-Dialog')).to_have_count(0)
    print('PASS saved-file changes update current readonly definition/completion; memory and failed reload do not; deleted source is unavailable', flush=True)

    # Two frontend notebook adapters on one actual kernel retain separate locals.
    page.evaluate('window.__onecFirst = window.jupyterapp.shell.currentWidget; void 0')
    first_kernel = page.evaluate('window.__onecFirst.sessionContext.session.kernel.id')
    page.evaluate('''async kernel => {
      const app = window.jupyterapp;
      const model = await app.serviceManager.contents.newUntitled({type:'notebook'});
      await app.serviceManager.sessions.startNew({path:model.path, name:'', type:'notebook', kernel:{id:kernel, name:'python3'}});
      const widget = await app.commands.execute('docmanager:open', {path:model.path});
      await widget.context.ready;
      await widget.sessionContext.ready;
      const cells = widget.content.model.sharedModel;
      while (cells.cells.length < 3) cells.insertCell(cells.cells.length, {cell_type:'code', source:''});
      window.__onecSecond = widget;
      app.shell.activateById(widget.id);
    }''', first_kernel)
    page.wait_for_function('window.jupyterapp.shell.currentWidget === window.__onecSecond', timeout=5000)
    expect(page.locator('.jp-CodeCell .cm-content:visible')).to_have_count(3, timeout=20000)
    edit(page, 1, '%%bsl\nПроцедура NotebookLocalBeta()\nКонецПроцедуры\n')
    completion(page, prefix, 'ДисковыйБ')
    completion(page, '%%bsl\nNotebookLoc', 'NotebookLocalBeta', 'NotebookLocalAlpha')
    print('PASS two notebooks share project state and isolate local declarations', flush=True)
    # Start an independent session. Jupyter's changeKernel shuts down the old
    # shared kernel, so it cannot establish simultaneous two-kernel isolation.
    page.evaluate('''async () => {
      const app = window.jupyterapp;
      const model = await app.serviceManager.contents.newUntitled({type:'notebook'});
      await app.serviceManager.sessions.startNew({path:model.path, name:'', type:'notebook', kernel:{name:'python3'}});
      const widget = await app.commands.execute('docmanager:open', {path:model.path});
      await widget.context.ready;
      await widget.sessionContext.ready;
      const cells = widget.content.model.sharedModel;
      while (cells.cells.length < 3) cells.insertCell(cells.cells.length, {cell_type:'code', source:''});
      window.__onecIndependent = widget;
      app.shell.activateById(widget.id);
    }''')
    page.wait_for_function('window.jupyterapp.shell.currentWidget === window.__onecIndependent')
    expect(status).to_contain_text('virtual only', timeout=30000)
    assert page.evaluate('window.__onecIndependent.sessionContext.session.kernel.id') != first_kernel
    execute(page, startup)
    execute(page, reload_code('ВторойKernel', 1))
    completion(page, prefix, 'ДисковыйБ', 'ВторойKernel')
    page.evaluate('window.jupyterapp.shell.activateById(window.__onecFirst.id)')
    completion(page, prefix, 'ДисковыйБ', 'ВторойKernel')
    other = copy_source_fixture(source_root, source_root.parent / 'other-project')
    other_oracle = SourceWriteOracle(other)
    other_module = other / module.relative_to(source_root)
    other_oracle.save(other_module, disk_source('ДругойПроект'))
    page.evaluate('window.jupyterapp.shell.activateById(window.__onecIndependent.id)')
    execute(page, '_lsp_runtime.close()')
    execute(page, startup.replace(repr(str(source_root)), repr(str(other))))
    completion(page, prefix, 'ДругойПроект', 'ДисковыйБ')
    page.evaluate('window.jupyterapp.shell.activateById(window.__onecFirst.id)')
    completion(page, prefix, 'ДисковыйБ', 'ДругойПроект')
    other_oracle.check()
    print('PASS shared-kernel locals, two kernels on one root, independent different roots and late attachment', flush=True)

    # Rename and reconnect must discover current kernel state without startup.
    page.evaluate('''async () => { await window.__onecFirst.context.rename('renamed.ipynb'); await window.__onecFirst.context.save(); }''')
    completion(page, prefix, 'ДисковыйБ')
    page.reload()
    page.wait_for_function('!!window.jupyterapp', timeout=60000)
    page.evaluate('''async () => {
      const app = window.jupyterapp;
      await app.restored;
      const widget = await app.commands.execute('docmanager:open', {path:'renamed.ipynb'});
      await widget.context.ready;
      await widget.sessionContext.ready;
      app.shell.activateById(widget.id);
    }''')
    expect(page.locator('.jp-CodeCell .cm-content:visible')).to_have_count(3, timeout=60000)
    expect(status).to_contain_text('project', timeout=30000)
    completion(page, prefix, 'ДисковыйБ')
    print('PASS rename and browser reconnect preserve authoritative current state', flush=True)
    execute(page, reload_code('Неизвестный', 4, failure='unknown'))
    completion(page, prefix, 'ДисковыйБ', 'Неизвестный')
    print('PASS failed unknown runtime promotion leaves saved-file analysis available', flush=True)
    execute(page, '_lsp_runtime.close()')
    expect(status).to_contain_text('project', timeout=30000)
    completion(page, prefix, 'ДисковыйБ')
    execute(page, rootless)
    expect(status).to_contain_text('virtual only', timeout=30000)
    execute(page, startup)
    expect(status).to_contain_text('project', timeout=30000)
    # Construct valid config first, then make only our owned export unavailable
    # before attachment; reproduces a kernel/server filesystem visibility mismatch.
    execute(page, '''from tempfile import TemporaryDirectory
_remote_fixture = TemporaryDirectory(prefix='onec-remote-source-')
_remote_root = Path(_remote_fixture.name)
(_remote_root / 'CommonModules').mkdir()
_remote_config = replace(_lsp_config, source_root=_remote_root)
_remote_api = _semantic_snapshot_runtime(_lsp_work, _common_module_catalog('ProbeServer'))
_remote_runtime = RuntimeSession(_remote_config, _Closeable(), _Closeable(), _IdleRdbg(), _remote_api, SimpleNamespace())
_remote_fixture.cleanup()
install_runtime(get_ipython(), _remote_runtime)
''')
    expect(status).to_contain_text('virtual only', timeout=30000)
    expect(status).to_contain_text('unavailable', timeout=30000)
    completion(page, '%%bsl\nСообщ', 'Сообщить')
    execute(page, startup)
    completion(page, prefix, 'ДисковыйБ')
    page.evaluate('async () => { await window.jupyterapp.shell.currentWidget.sessionContext.session.kernel.restart(); }')
    expect(status).to_contain_text('virtual only', timeout=30000)
    completion(page, '%%bsl\nСообщ', 'Сообщить')
    execute(page, startup)
    completion(page, prefix, 'ДисковыйБ')
    prior_kernel = page.evaluate('window.jupyterapp.shell.currentWidget.sessionContext.session.kernel.id')
    page.evaluate('async () => { await window.jupyterapp.shell.currentWidget.sessionContext.changeKernel({name:"python3"}); }')
    assert page.evaluate('window.jupyterapp.shell.currentWidget.sessionContext.session.kernel.id') != prior_kernel
    expect(status).to_contain_text('virtual only', timeout=30000)
    completion(page, '%%bsl\nСообщ', 'Сообщить')
    print('PASS close retains project; None/unavailable root/reinstall/restart/kernel change obey configuration lifetime', flush=True)
    oracle.save(module, original_module)
    assert fingerprints(source_root) == source_files
    assert {p: sha256(p.read_bytes()).hexdigest() for p in notebook_root.rglob('*.bsl')} == notebook_sources
    saved = json.loads((notebook_root / 'renamed.ipynb').read_text(encoding='utf-8'))
    assert all(not cell.get('outputs') for cell in saved['cells'])
    assert 'binding_id' not in json.dumps(saved['metadata'])
    print('PASS exact fixture restoration, no virtual files, source outputs or binding metadata', flush=True)
    return {'backend':'test-runtime-real-kernel-browser-native-lsp', 'checks':[
        'rootless-builtins-locals-hover-diagnostics', 'header-and-drag-hover', 'null-root',
        'automatic-project-binding', 'disk-edit-independent-of-memory-reload', 'failed-reload-preserves-disk-analysis',
        'current-readonly-definitions-deletion-unavailable', 'shared-kernel-local-isolation', 'same-root-two-kernels',
        'native-source-viewer-alias-refresh-close-reopen-no-modal-or-context',
        'different-root-isolation', 'rename-reconnect-late-attachment', 'unknown-runtime-outcome-independent',
        'runtime-close-retains-root', 'none-reinstall-unavailable-host-root-restart-kernel-change', 'no-source-writes-or-source-persistence'],
        'live_1c':'UNVERIFIED', 'runtime_budget':'SEPARATE_LIVE_GATE'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('artifacts/lsp-runtime'))
    parser.add_argument('--channel', default='msedge')
    parser.add_argument('--layout', choices=['designer', 'edt-parent', 'edt-src', 'all'], default='designer')
    args = parser.parse_args()
    command = [sys.executable, str(Path(__file__).with_name('check_jupyter_lsp_browser.py')),
        '--server', str(args.server.resolve()), '--output', str(args.output.resolve()),
        '--channel', args.channel, '--runtime-acceptance']
    layouts = ['designer', 'edt-parent', 'edt-src'] if args.layout == 'all' else [args.layout]
    from check_jupyter_bsl_lsp import create_edt_fixture
    for layout in layouts:
        print('LAYOUT ' + layout, flush=True)
        with TemporaryDirectory(prefix='onec-runtime-edt-') as temporary:
            extra = []
            if layout != 'designer':
                root = Path(temporary)
                create_edt_fixture(root, 'JupyterBslFixtureCalleeServer')
                extra = ['--source-root', str(root / 'src' if layout == 'edt-src' else root)]
            subprocess.run([*command, '--output', str(args.output.resolve() / layout), *extra], check=True)


if __name__ == '__main__':
    main()
