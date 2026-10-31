"""Cross-platform local launcher; leaves an existing runtime DB untouched."""
import argparse
import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description='김장철병원 로컬 DB 대시보드')
    parser.add_argument('--port', type=int, default=8767)
    parser.add_argument('--check', action='store_true', help='환경과 필수 파일만 확인')
    parser.add_argument('--current-python', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('포트는 1~65535 사이여야 합니다.')
    python = ROOT / 'backend/.venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    if not args.current_python and python.exists():
        result = subprocess.run([str(python), str(Path(__file__).resolve()), *sys.argv[1:], '--current-python'], cwd=ROOT)
        raise SystemExit(result.returncode)
    if sys.version_info < (3, 12):
        raise SystemExit('Python 3.12 이상이 필요합니다. 검증 환경은 Python 3.13입니다.')
    for name in ['fastapi', 'sqlmodel', 'uvicorn', 'httpx']:
        try:
            importlib.import_module(name)
        except ImportError:
            raise SystemExit(f'{name} 설치가 필요합니다. 먼저 python setup.py 를 실행하세요.')
    required = ['dist/index.html', 'backend/app.py', 'backend/fixtures/demo.json',
                'backend/fixtures/charts-v1.json', 'backend/schemas/patient-chart-v1.template.json',
                'backend/fixtures/rag/medical-knowledge-source.txt', 'backend/fixtures/rag/visit-preparation-extra.txt',
                'backend/fixtures/rag/preparation-policies.json', 'backend/fixtures/rag/vocabulary-levels.json']
    missing = [name for name in required if not (ROOT / name).is_file()]
    if missing:
        raise SystemExit('필수 파일이 없습니다. 압축을 모두 풀어 주세요: ' + ', '.join(missing))
    print(f'환경 확인 완료: Python {sys.version.split()[0]} · 필수 파일 정상', flush=True)
    if args.check:
        return
    if os.environ.get('HOSPITAL_DB_PATH'):
        target = Path(os.environ['HOSPITAL_DB_PATH']).expanduser().resolve()
    else:
        target = ROOT / 'backend/.local-data/hospital.sqlite3'
    target.parent.mkdir(parents=True, exist_ok=True)
    snapshot = ROOT / 'data/hospital-demo.sqlite3'
    if not target.exists() and snapshot.is_file():
        # Exclusive creation prevents a simultaneous launcher from overwriting an existing DB.
        try:
            with target.open('xb') as destination, snapshot.open('rb') as source:
                shutil.copyfileobj(source, destination)
            print('공유 DB 사본을 최초 실행용 DB로 복사했습니다.', flush=True)
        except FileExistsError:
            pass
    env = dict(os.environ)
    env['HOSPITAL_DB_PATH'] = str(target)
    print(f'화면: http://127.0.0.1:{args.port}/\nAPI 문서: http://127.0.0.1:{args.port}/docs\nDB: {target}\n종료: Ctrl+C', flush=True)
    try:
        result = subprocess.run([sys.executable, '-m', 'uvicorn', 'backend.app:app', '--host', '127.0.0.1',
                                 '--port', str(args.port)], cwd=ROOT, env=env)
        raise SystemExit(result.returncode)
    except KeyboardInterrupt:
        print('\n서버를 종료합니다.', flush=True)


if __name__ == '__main__':
    main()
