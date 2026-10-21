# Build with tools/build_windows.py in the dedicated, pinned build environment.
from pathlib import Path
import ast
import sys

root = Path(SPECPATH).parent
sys.path.insert(0, str(root / 'tools'))
from build_release import PUBLIC_DOCS, permitted
generated = root / '.local' / 'build'
resources = []
for name in ('web', 'vendor', 'skills', '.claude-plugin', 'evaluation', 'cpp'):
    for path in sorted((root / name).rglob('*')):
        if not path.is_file() or not permitted(path.relative_to(root)):
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f'Resource escapes the application: {path}')
        resources.append((str(path), 'editor/'+path.parent.relative_to(root).as_posix()))
resources += [(str(root / 'docs' / name), 'editor/docs') for name in PUBLIC_DOCS if (root / 'docs' / name).is_file()]
resources += [(str(root / name), 'editor') for name in ('profiles.json', 'LICENSE', 'README.md', 'runtime_paths.py', 'decider_errors.py')]
resources += [(str(root / name), 'editor') for name in ('verification.json', 'plugin.json', 'THIRD_PARTY_NOTICES.md', 'CONTRIBUTING.md', 'SECURITY.md') if (root / name).is_file()]
resources += [(str(root / 'tools' / name), 'editor/tools') for name in ('decider_worker.py', 'setup_decider.py', 'download_decider.py', 'setup_cpp_decider.py', 'download_cpp_decider.py', 'build_cpp_decider.py') if (root / 'tools' / name).is_file()]
cpp_runtime = root / '.local/decider-cpp/runtime'
if cpp_runtime.is_dir():
    # Deliberately package the allowlisted sidecar only, never its source clone,
    # build cache, weights or user settings.
    import json, hashlib
    metadata = json.loads((cpp_runtime / 'BUILD.json').read_text('utf-8'))
    for item in metadata['files']:
        path = cpp_runtime / 'bin' / item['file']
        if path.parent != cpp_runtime / 'bin' or not path.is_file():
            raise ValueError('Invalid C++ runtime inventory')
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != item['sha256']:
                raise ValueError('C++ runtime hash mismatch')
        resources.append((str(path), 'editor/cpp/runtime/bin'))
    resources.append((str(cpp_runtime / 'BUILD.json'), 'editor/cpp/runtime'))
    resources.append((str(cpp_runtime / 'licenses'), 'editor/cpp/runtime/licenses'))
notices = generated / 'third_party_licenses'
if notices.is_dir():
    resources += [(str(notices), 'editor/third_party_licenses')]

# The unmodified checker dynamically imports/executes its sibling scripts.
# Analyze their stdlib imports too, instead of depending on the developer's Python.
vendor_imports = set()
for source in (root / 'vendor' / 'yomiyasu' / 'scripts').glob('*.py'):
    for node in ast.walk(ast.parse(source.read_text('utf-8'))):
        if isinstance(node, ast.Import):
            vendor_imports.update(item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            vendor_imports.add(node.module)
vendor_imports.update({'yomiyasu_check', 'yomiyasu_lint', 'yomiyasu_diff', 'markdown_visibility'})
vendor_path = str(root / 'vendor' / 'yomiyasu' / 'scripts')
excludes = ['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'cefpython3', 'torch', 'transformers', 'numpy', 'tkinter']
gui = Analysis([str(root / 'desktop.py')], pathex=[str(root), vendor_path],
               datas=resources, hiddenimports=sorted(vendor_imports | {'webview.platforms.winforms', 'webview.platforms.edgechromium'}),
               excludes=excludes)
cli = Analysis([str(root / 'app.py')], pathex=[str(root), vendor_path],
               datas=resources, hiddenimports=sorted(vendor_imports), excludes=excludes+['webview'])

# bindepend also discovers the sidecar's PE imports at _internal's root. The
# external worker resolves its own app-local directory; Python never imports
# these libraries. Keep one copy beside the worker, retaining the main app's
# CRT/Python/WebView libraries at the root.
sidecar_only = {'llama.dll', 'ggml.dll', 'ggml-base.dll', 'ggml-cpu.dll', 'ggml-cuda.dll',
                'cudart64_12.dll', 'cublas64_12.dll', 'cublasLt64_12.dll'}
gui.binaries = [row for row in gui.binaries if row[0] not in sidecar_only]
cli.binaries = [row for row in cli.binaries if row[0] not in sidecar_only]
gui_exe = EXE(PYZ(gui.pure), gui.scripts, [], exclude_binaries=True, name='YomiyasuStudio',
              console=False, icon=str(generated / 'icon.ico'), uac_admin=False)
cli_exe = EXE(PYZ(cli.pure), cli.scripts, [], exclude_binaries=True, name='YomiyasuStudioCLI',
              console=True, icon=str(generated / 'icon.ico'), uac_admin=False)
COLLECT(gui_exe, cli_exe, gui.binaries, gui.datas, cli.binaries, cli.datas,
        strip=False, upx=False, name='YomiyasuStudio')
