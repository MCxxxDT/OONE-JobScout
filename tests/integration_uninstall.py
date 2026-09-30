"""Deletes only disposable fixtures created in its TemporaryDirectory."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

source = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
with tempfile.TemporaryDirectory(prefix='oone-e2e-中文 ') as folder:
    root = Path(folder).resolve() / 'project with spaces'
    root.mkdir()
    (root / 'boss_apply').mkdir()
    (root / 'boss_apply/__init__.py').write_text('')
    (root / 'boss_apply/config.py').write_text('')
    (root / 'boss_apply/fixture_service.py').write_text('import time\ntime.sleep(120)\n')
    (root / 'pyproject.toml').write_text('[project]\nname = "oone-jobscout"\n')
    (root / 'start.sh').write_text('')
    (root / 'start.bat').write_text('')
    (root / 'state/browser-profile').mkdir(parents=True)
    (root / 'state/browser-profile/Cookies').write_text('fixture only')
    (root / 'config.local.json').write_text('fixture only')
    shutil.copyfile(source / 'boss_apply/uninstall.py', root / 'boss_apply/uninstall.py')
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(root / '.venv')], check=True)
    python = root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    service = subprocess.Popen([str(python), '-B', '-m', 'boss_apply.fixture_service'], cwd=root)
    browser = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(120)',
                                '--user-data-dir=' + str(root / 'state/browser-profile')])
    unrelated = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(120)', '--remote-debugging-port=9335'])
    try:
        time.sleep(.3)
        before = subprocess.run([str(python), '-B', str(root / 'boss_apply/uninstall.py'), '--dry-run'], capture_output=True, text=True, encoding="utf-8")
        assert before.returncode == 0, before.stderr
        assert f'PID {service.pid}' in before.stdout, before.stdout
        assert f'PID {browser.pid}' in before.stdout, before.stdout
        assert f'PID {unrelated.pid}' not in before.stdout, before.stdout
        assert service.poll() is None and browser.poll() is None
        completed = subprocess.run([str(python), '-B', str(root / 'boss_apply/uninstall.py'), '--yes'], cwd=Path(folder), capture_output=True, text=True, encoding="utf-8", timeout=90)
        print(completed.stdout)
        assert completed.returncode == 0, completed.stderr
        assert not root.exists(), root
        assert service.wait(timeout=3) is not None
        assert browser.wait(timeout=3) is not None
        assert unrelated.poll() is None
        print('PASS: .venv self-uninstall; path with spaces/Unicode; two scoped processes stopped; unrelated process preserved; no fixture files remain.')
    finally:
        for process in (service, browser, unrelated):
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
