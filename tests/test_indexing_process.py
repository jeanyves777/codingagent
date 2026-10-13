"""Repository indexing must survive native parser GC in a real Python process."""
import json
import os
from pathlib import Path
import subprocess
import sys

from brain.intelligence import build_index


def test_index_prunes_excluded_folders_and_hidden_files(tmp_path):
    (tmp_path / 'main.py').write_text('def visible():\n    return 1\n')
    for directory in ('.hidden', 'dist', '.venv', 'node_modules'):
        folder = tmp_path / directory
        folder.mkdir()
        (folder / 'ignored.py').write_text('def ignored():\n    return 2\n')
    (tmp_path / '.hidden.py').write_text('def hidden():\n    return 3\n')
    result = build_index(tmp_path)
    assert [(s['path'], s['name']) for s in result['symbols']] == [('main.py', 'visible')]


def test_index_full_repository_survives_collection():
    root = Path(__file__).resolve().parents[1]
    script = '''
import gc, json
from pathlib import Path
from brain.intelligence import build_index
gc.set_threshold(100, 5, 5)
for _ in range(2):
    result = build_index(Path.cwd())
    assert any(s['path'] == 'brain/intelligence.py' and s['name'] == 'build_index'
               for s in result['symbols'])
    assert any(s['path'] == 'desktop_ui/static/app.js' for s in result['symbols'])
    gc.collect()
print(json.dumps({'symbols': len(result['symbols']), 'calls': len(result['calls'])}))
'''
    process = subprocess.run([sys.executable, '-X', 'faulthandler', '-c', script],
        cwd=root, env={**os.environ, 'PYTHONPATH': str(root)},
        capture_output=True, text=True, timeout=180)
    assert process.returncode == 0, f'exit {process.returncode}: {process.stderr}'
    assert json.loads(process.stdout)['symbols'] > 100
