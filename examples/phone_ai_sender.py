"""Send synthetic phone-AI events using only the Python standard library."""
import argparse
import json
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import uuid4


def make_events(call_id):
    payloads = [
        ('call_started', {'patient_id': 'demo-patient-003', 'caller_key': 'demo-caller-3', 'name': '이정례',
                          'age': 82, 'phone': '010-0000-1003', 'region': '화순군 화순읍', 'purpose': '피검사 방문 준비',
                          'visit_type': '재진', 'hospital_code': 'KJC_DEMO', 'planned_visit_text': '가상 통화 시연'}),
        ('location_observed', {'latitude': 35.057, 'longitude': 126.977, 'location_source': 'simulated', 'accuracy_m': None}),
        ('message_finalized', {'sequence': 1, 'speaker': 'assistant', 'text': '방문 준비를 도와드릴게요. 어떤 내용을 확인하고 싶으세요?'}),
        ('message_finalized', {'sequence': 2, 'speaker': 'caller', 'text': '내일 피검사 하러 가려고 해요. 준비할 것을 다시 확인하고 싶어요.'}),
        ('call_ended', {}),
        ('summary_ready', {'summary_text': '가상 환자가 피검사 방문 전 준비 안내를 재확인했습니다. 실제 검사 준비는 병원에 확인할 질문으로 정리합니다.'}),
    ]
    return [{'source': 'phone-ai-example', 'event_id': f'{call_id}-event-{i+1}', 'call_id': call_id,
             'event_type': kind, 'occurred_at': datetime.now(timezone.utc).isoformat(), 'payload': payload}
            for i, (kind, payload) in enumerate(payloads)]


def main():
    parser = argparse.ArgumentParser(description='전화 AI → 대시보드 가상 이벤트 전송 예제')
    parser.add_argument('--base-url', default='http://127.0.0.1:8767')
    parser.add_argument('--call-id', default=None)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    call_id = args.call_id or f'phone-example-{uuid4()}'
    events = make_events(call_id)
    if args.dry_run:
        print(json.dumps(events, ensure_ascii=False, indent=2))
        return
    base = args.base_url.rstrip('/')
    try:
        for event in events:
            # The same serialized request must be reused on a retry; do not regenerate event_id/timestamp.
            request = Request(base + '/api/team/call-events', data=json.dumps(event, ensure_ascii=False).encode('utf-8'),
                              headers={'Content-Type': 'application/json'}, method='POST')
            with urlopen(request, timeout=10) as response:
                result = json.load(response)
            print(f"{event['event_type']}: applied={result.get('applied')}, duplicate={result.get('duplicate', False)}")
        with urlopen(base + '/api/team/calls/' + quote(call_id, safe='') + '/preparation', timeout=10) as response:
            result = json.load(response)
        print(json.dumps({'call_id': call_id, 'preparation_document_ids': [c['document_id'] for c in result['cards']]}, ensure_ascii=False, indent=2))
    except HTTPError as error:
        raise SystemExit(f'HTTP {error.code}: {error.read().decode("utf-8", errors="replace")}')
    except URLError as error:
        raise SystemExit(f'연결 실패. 서버와 주소를 확인하세요. 같은 요청을 재전송할 때 ID와 본문을 유지하세요: {error.reason}')


if __name__ == '__main__':
    main()
