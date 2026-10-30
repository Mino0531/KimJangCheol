# 김장철병원 팀원 개발·GitHub 협업 가이드

저장소: https://github.com/Mino0531/KimJangCheol

기본 흐름은 **복제 → 실행 → 내 브랜치에서 수정 → 확인 → 커밋·푸시 → PR → 검토 후 합치기**입니다. PR(Pull Request)은 내 브랜치의 변경을 공통 코드인 main에 합쳐 달라는 요청입니다.

## 1. 처음 준비하기

- Git과 Python 3.13을 설치합니다. 앱 최소 버전은 Python 3.12이며, 직접 검증한 환경은 macOS·Python 3.13.2입니다.
- 저장소 초대 메일을 수락합니다. 공개 저장소의 코드를 내려받는 것은 초대 없이 가능하지만, 이 저장소에 브랜치를 올리려면 쓰기 권한이 필요합니다.
- Windows는 PowerShell 또는 Git Bash, macOS는 터미널을 사용합니다.
- 프론트엔드를 수정할 사람은 Node.js 20 이상도 준비합니다. 기본 앱 실행에는 Node.js나 AWS API 키가 필요하지 않습니다.

계속 코드를 수정할 예정이면 아래 git clone을 사용하세요. GitHub의 Download ZIP에는 Git 이력이 없으므로, ZIP으로 실행하던 팀원도 협업용으로는 새로 복제하고 필요한 수정 파일만 옮겨 주세요.

## 2. 저장소 내려받기 — 최초 한 번

프로젝트를 보관할 폴더에서 실행합니다.

```sh
git clone https://github.com/Mino0531/KimJangCheol.git
cd KimJangCheol
```

이후 명령은 모두 이 KimJangCheol 폴더 안에서 실행합니다.

처음 커밋하기 전에 이 저장소에서 사용할 이름과 이메일을 설정합니다. 아래 값은 본인 정보로 바꿉니다. 이메일은 GitHub에 등록한 주소 또는 GitHub 설정의 noreply 주소를 사용합니다.

```sh
git config user.name "본인 이름"
git config user.email "본인의 커밋용 이메일"
```

이 설정은 GitHub 로그인과 별개입니다. 푸시 시 인증을 요청받으면 본인 GitHub 계정으로 인증합니다. GitHub Desktop을 사용하는 팀원은 앱에 로그인해 복제·커밋·푸시해도 됩니다.

## 3. 설치하고 실행하기

macOS / Linux:

```sh
python3 setup.py
python3 run.py
```

Windows:

```text
py setup.py
py run.py
```

setup.py는 최초 한 번 실행합니다. 이후에는 run.py만 실행하면 됩니다. Python 의존성 파일이 변경되면 setup.py를 다시 실행합니다.

- 대시보드: http://127.0.0.1:8767/
- 지자체 대시보드: http://127.0.0.1:8767/municipality
- API 문서·요청 테스트: http://127.0.0.1:8767/docs
- 종료: 서버를 실행한 터미널에서 Ctrl+C
- 포트가 사용 중이면 macOS는 `python3 run.py --port 8777`, Windows는 `py run.py --port 8777`로 실행하고 http://127.0.0.1:8777/ 에 접속합니다.

서버용 터미널은 열어 두고, Git 명령은 같은 폴더에서 별도 터미널을 열어 실행하면 편합니다.

## 4. 작업 시작할 때 — 최신 main에서 내 브랜치 만들기

먼저 git status로 확인합니다. 진행 중인 수정이 있으면 현재 브랜치에서 저장·커밋한 뒤 이동하세요. 아래 순서는 작업 폴더에 미커밋 변경이 없는 상태를 전제로 합니다.

```sh
git status
git switch main
git pull --ff-only origin main
git switch -c feature/minho-phone-api
```

feature/minho-phone-api는 예시입니다. 본인 이름과 작업 내용에 맞게 바꾸고, 아래 푸시 명령에도 같은 이름을 사용하세요. 새 작업에는 새 브랜치를 만듭니다.

| 담당 작업 | 브랜치 이름 예시 | 주요 파일 |
|---|---|---|
| 전화 AI API 연결 | feature/jiyoun-phone-api | examples/, docs/PHONE_AI_API.md, backend/app.py |
| 대시보드 화면 수정 | feature/dongwook-dashboard | src/, dist/index.html |
| 방문 준비·RAG 개선 | feature/minho-rag | backend/preparation.py, backend/fixtures/rag/ |

브랜치 이름은 팀에서 정하는 규칙이며, 기능별 브랜치에 작업을 모아 PR로 합칩니다.

## 5. 코드 수정 후 확인하기

프론트엔드는 src/를 수정한 다음, 저장소 루트에서 HTML을 다시 빌드합니다.

```sh
node scripts/build.mjs
```

병원은 dist/index.html, 지자체는 dist/municipality.html을 제공합니다. src/만 수정하면 실제 화면에 반영되지 않으므로, 변경된 빌드 결과 dist/index.html과 dist/municipality.html도 함께 커밋합니다. 브라우저를 새로고침해 결과를 확인합니다.

백엔드는 수정 후 서버를 Ctrl+C로 종료하고 run.py로 다시 실행합니다. DB·API 로직을 바꿨다면 다음 테스트를 실행합니다.

macOS / Linux:

```sh
backend/.venv/bin/python -m unittest backend.test_coordination backend.test_preparation backend.test_knowledge backend.test_app backend.test_chart_format -v
```

Windows:

```text
backend\.venv\Scripts\python.exe -m unittest backend.test_coordination backend.test_preparation backend.test_knowledge backend.test_app backend.test_chart_format -v
```

테스트는 임시 DB를 사용합니다. 각자 실행 중인 DB는 초기화하지 않습니다. 전화 AI 연결은 docs/PHONE_AI_API.md와 examples/phone_ai_sender.py를 참고합니다.

## 6. 수정한 코드를 GitHub에 올리기

다음은 백엔드 API와 문서를 수정한 예시입니다. git add 뒤 파일 경로는 본인이 실제 수정한 파일로 바꿉니다.

```sh
git status
git diff
git add backend/app.py docs/PHONE_AI_API.md
git diff --cached
git commit -m "전화 AI 이벤트 연결 수정"
git push -u origin feature/minho-phone-api
```

화면을 수정했다면 예를 들어 `git add src/app.js src/styles.css dist/index.html`처럼 수정 소스와 빌드 결과를 선택합니다.

git commit은 내 PC에 변경 이력을 저장하고, git push는 그 이력을 GitHub에 올립니다. -u는 해당 브랜치의 첫 푸시에 사용합니다. 같은 브랜치에서 추가 수정 후에는 커밋하고 git push만 실행하면 됩니다.

## 7. PR을 만들어 팀 코드에 합치기

1. https://github.com/Mino0531/KimJangCheol 에 접속합니다.
2. Compare & pull request를 누릅니다. 보이지 않으면 Pull requests → New pull request를 선택합니다.
3. **base: main**, **compare: 본인 작업 브랜치**인지 확인합니다.
4. 제목과 설명을 작성하고 Create pull request를 누릅니다.
5. 팀원이 변경 내용을 확인한 뒤 Merge pull request로 합칩니다. 푸시만으로 main에 반영되지는 않습니다.

PR 설명은 다음 형식이면 충분합니다.

```text
변경 내용: 무엇을 바꿨는지
확인 방법: 실행·화면·API·테스트에서 확인한 내용
팀원 영향: 변경된 API 필드, 의존성, 데이터 구조가 있는지
```

수정 요청을 받으면 같은 작업 브랜치에서 수정 → 커밋 → 푸시합니다. 열린 PR에 자동으로 추가됩니다.

## 8. 다른 팀원 코드를 내려받기

현재 작업을 커밋하고 main으로 이동해 최신 코드를 받습니다.

```sh
git switch main
git pull --ff-only origin main
```

의존성이 변경됐다면 setup.py를 다시 실행하고 서버를 재시작합니다. 다음 기능은 최신 main에서 새 브랜치를 만들어 시작합니다.

내 작업 브랜치에도 최신 main이 필요하다면, 먼저 수정 내용을 커밋한 상태에서 다음을 실행합니다.

```sh
git fetch origin
git merge origin/main
```

충돌이 나면 충돌 파일을 열어 팀원 변경과 내 변경을 비교하고 올바른 내용으로 정리합니다. 충돌 표시를 제거한 후 해당 파일을 git add하고 git commit, git push합니다. 병합을 취소하려면 git merge --abort를 사용합니다. SQLite 같은 바이너리 파일의 충돌은 텍스트처럼 합칠 수 없으므로 담당자와 사본을 정합니다.

## 9. 이 프로젝트의 DB와 공유 파일

- backend/.local-data/hospital.sqlite3는 각자 실행 중인 DB입니다. .gitignore로 제외되어 메모·추가 통화 등 DB 내용은 git push/pull로 공유되지 않습니다.
- data/hospital-demo.sqlite3는 배포용 가상 DB 사본이며 Git에 포함되어 있습니다. 실행용 DB를 이 파일 위에 덮어써서 올리지 마세요. 사본 갱신은 담당자가 가상 데이터 여부와 관련 내보내기 파일을 함께 확인한 뒤 진행합니다.
- 새 사본을 내려받아도 run.py는 기존 실행용 DB를 덮어쓰지 않습니다. 따라서 새 가상 데이터가 기존 DB에 자동 반영되지는 않습니다. 필요한 경우 담당자와 반영 방법을 정합니다.
- backend/fixtures/는 가상 환자·통화·RAG 원본입니다. 원본 파일을 수정했다고 기존 DB가 자동으로 바뀌지는 않습니다.
- backend/.venv/와 캐시는 올리지 않습니다. API 키·인증 파일·실제 환자 정보를 커밋하지 않습니다.
- downloads/의 ZIP은 특정 시점의 전달본입니다. 소스를 수정해도 ZIP은 자동 갱신되지 않습니다. 협업 중 최신 코드는 main을 기준으로 받습니다.

## 10. 막혔을 때

| 상황 | 확인할 내용 |
|---|---|
| git 명령이 없음 | Git 설치 후 터미널을 다시 열기 |
| 푸시할 때 403 또는 권한 오류 | 저장소 초대 수락 여부와 인증한 GitHub 계정 확인 |
| 커밋할 때 이름·이메일 오류 | 2번의 git config 설정 확인 |
| 브랜치 이동·pull이 로컬 수정 때문에 중단됨 | git status 확인 후 현재 브랜치에 작업을 커밋하고 재시도 |
| push가 rejected됨 | git fetch origin으로 원격 변경 확인 후 병합. force push로 덮어쓰지 않기 |
| 화면이 바뀌지 않음 | node scripts/build.mjs 실행, 서버 주소 확인, 새로고침 |
| 팀원과 통화·메모 내용이 다름 | 각 PC가 별도 SQLite DB를 사용하므로 공용 DB 상태가 아님 |

## 참고

- 저장소 실행 상세: README.md
- 전화 AI API 계약: docs/PHONE_AI_API.md
- [GitHub 공식: 저장소 복제](https://docs.github.com/en/repositories/creating-and-managing-repositories/cloning-a-repository)
- [GitHub 공식: PR 빠른 시작](https://docs.github.com/en/pull-requests/get-started/pull-request-quickstart)
