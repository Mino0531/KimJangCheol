"""One-time local setup. Run with Python 3.12 or newer (tested with 3.13)."""
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    if sys.version_info < (3, 12):
        raise SystemExit('Python 3.12 이상이 필요합니다. 검증 환경은 Python 3.13입니다.')
    folder = ROOT / 'backend/.venv'
    python = folder / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    if not python.exists():
        print('프로젝트 전용 Python 환경을 만듭니다.', flush=True)
        venv.EnvBuilder(with_pip=True).create(folder)
    subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
                    '-r', str(ROOT / 'backend/requirements.lock.txt')], cwd=ROOT, check=True)
    subprocess.run([str(python), str(ROOT / 'run.py'), '--check'], cwd=ROOT, check=True)
    print('\n설치 완료. python run.py 로 실행하세요.', flush=True)


if __name__ == '__main__':
    main()
