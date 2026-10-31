"""Build the offline demo's preparation cards from the same DB retrieval code."""
import json
import tempfile
from pathlib import Path
from fastapi.testclient import TestClient
from backend.app import create_app


def main():
    with tempfile.TemporaryDirectory() as directory:
        with TestClient(create_app(Path(directory) / 'fixture.sqlite3')) as client:
            calls = client.get('/api/team/calls').json()
            result = {}
            for call in calls:
                response = client.get(f"/api/team/calls/{call['id']}/preparation")
                response.raise_for_status()
                result[call['id']] = response.json()
            output = Path(__file__).parent / 'fixtures/rag/preparation-display.json'
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
            print(f'Exported preparation fixtures for {len(result)} calls')


if __name__ == '__main__':
    main()
