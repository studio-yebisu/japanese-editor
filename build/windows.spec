# Build with tools/build_windows.py in the dedicated, pinned build environment.
from pathlib import Path
import ast
import sys

root = Path(SPECPATH).parent
sys.path.insert(0, str(root / 'tools'))
from build_release import PUBLIC_DOCS, permitted
generated = root / '.local' / 'build'
resources = []
for name in ('web', 'vendor', 'skills', '.claude-plugin', 'evaluation'):
    for path in sorted((root / name).rglob('*')):
        if not path.is_file() or not permitted(path.relative_to(root)):
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f'Resource escapes the application: {path}')
        resources.append((str(path), 'editor/'+path.parent.relative_to(root).as_posix()))
resources += [(str(root / 'docs' / name), 'editor/docs') for name in PUBLIC_DOCS if (root / 'docs' / name).is_file()]
resources += [(str(root / name), 'editor') for name in ('profiles.json', 'LICENSE', 'README.md', 'runtime_paths.py')]
resources += [(str(root / name), 'editor') for name in ('verification.json', 'plugin.json', 'THIRD_PARTY_NOTICES.md', 'CONTRIBUTING.md', 'SECURITY.md') if (root / name).is_file()]
resources += [(str(root / 'tools' / name), 'editor/tools') for name in ('decider_worker.py', 'setup_decider.py', 'download_decider.py')]
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
gui_exe = EXE(PYZ(gui.pure), gui.scripts, [], exclude_binaries=True, name='StudioYebisuEditor',
              console=False, icon=str(generated / 'icon.ico'), uac_admin=False)
cli_exe = EXE(PYZ(cli.pure), cli.scripts, [], exclude_binaries=True, name='StudioYebisuEditorCLI',
              console=True, icon=str(generated / 'icon.ico'), uac_admin=False)
COLLECT(gui_exe, cli_exe, gui.binaries, gui.datas, cli.binaries, cli.datas,
        strip=False, upx=False, name='StudioYebisuEditor')
