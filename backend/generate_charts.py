"""Generate deterministic synthetic charts, a fill-in template, and JSON Schema."""
from pathlib import Path
import copy
import json
from backend.chart_schema import ChartBundle

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'backend/fixtures/charts-v1.json'


def generate():
    fixture = json.loads((ROOT / 'backend/fixtures/demo.json').read_text())
    calls = {c['patient_id']: c for c in fixture['calls'] if c.get('patient_id')}
    patients = []
    for i, p in enumerate(fixture['patients']):
        c = calls[p['id']]
        medicines = [{'medication_id': m['id'], 'name': m['name'], 'purpose': m['purpose'],
                      'status': 'active', 'started_on': m['started_on'], 'ended_on': None, 'is_fictional': True}
                     for m in p['current_medications']]
        medicine_ids = {m['name']: m['medication_id'] for m in medicines}
        rx = [{'prescription_id': r['id'], 'prescribed_on': r['date'], 'department': r['department'],
               'institution': '기존 의료기관 · 가상',
               'items': [{'medication_id': medicine_ids[name], 'medication_name': name} for name in r['medications']]}
              for r in p['prescriptions']]
        if i == 2:
            medicines.append({'medication_id': 'demo-med-003-stopped', 'name': '이전시연정 Z',
                              'purpose': '과거 복용 기록 예시', 'status': 'stopped',
                              'started_on': '2026-07-17', 'ended_on': '2026-08-13', 'is_fictional': True})
            rx.append({'prescription_id': 'demo-rx-003-stopped', 'prescribed_on': '2026-07-17',
                       'department': '외래', 'institution': '기존 의료기관 · 가상',
                       'items': [{'medication_id': 'demo-med-003-stopped', 'medication_name': '이전시연정 Z'}]})
        allergy_status = 'recorded' if i == 0 else 'none_reported' if i == 4 else 'unknown'
        patients.append({
            'patient_id': p['id'], 'chart_number': p['chart_number'],
            'identity': {'name': c['name'], 'birth_date': f"{2026-c['age']}-01-15", 'sex': 'female' if p['sex'] == '여' else 'male',
                         'phone': c['phone'], 'region': c['region']},
            'provenance': {'as_of': '2026-10-23', 'recorded_at': '2026-10-30T14:00:00+09:00',
                           'source_kind': 'synthetic_fixture', 'verification_status': 'demo_verified', 'source_label': '공통 차트 v1 · 가상 수기 자료'},
            'conditions': {'knowledge_status': 'recorded' if p['conditions'] else 'unknown',
                           'items': [{'condition_id': f"{p['id']}-condition-{j+1}", 'name': x['name'], 'status': 'active',
                                      'diagnosed_on': f"{x['since'][:4]}-01-01", 'note': '시연용 지병 기록'} for j, x in enumerate(p['conditions'])]},
            'allergies': {'knowledge_status': allergy_status, 'items': [
                {'allergy_id': 'demo-allergy-001', 'substance': '페니실린계 · 가상 기록', 'reaction': None, 'recorded_on': '2026-07-17'}] if i == 0 else []},
            'medications': {'knowledge_status': 'recorded' if medicines else 'unknown', 'items': medicines},
            'prescriptions': rx,
            'observations': [{'observation_id': f"{p['id']}-obs-{j+1}", 'observed_on': o['date'],
                              'systolic': o['systolic'], 'diastolic': o['diastolic'], 'glucose': o['glucose'],
                              'blood_pressure_unit': 'mmHg', 'glucose_unit': 'mg/dL', 'glucose_context': 'unknown'}
                             for j, o in enumerate(p['observations'])],
            'encounters': [{'encounter_id': f"{p['id']}-enc-{j+1}", 'visited_on': e['date'], 'department': e['department'],
                            'institution': '기존 의료기관 · 가상', 'title': e['title'], 'note': e['note']}
                           for j, e in enumerate(p['encounters'])],
            'visit_preparation': {'mobility': 'needs_support' if i in [2, 6] else 'wheelchair' if i == 11 else 'independent',
                                  'hearing': 'needs_slow_speech' if i in [1, 2, 6, 11] else 'standard',
                                  'preferred_channel': 'voice', 'companion': 'not_planned' if i in [0, 2, 4] else 'unknown',
                                  'phone_type': 'feature_phone' if i in [1, 2, 6, 11] else 'smartphone',
                                  'staff_note': '짧은 문장으로 하나씩 안내하고, 병원에서 받은 안내문을 함께 확인합니다.' if i == 2 else '방문 준비에 필요한 안내를 차례로 확인합니다.'}
        })
    result = {'format_id': 'KJC_VISIT_PREPARATION_CHART', 'schema_version': '1.0', 'hospital_code': 'KJC_DEMO',
              'data_classification': 'synthetic', 'generated_at': '2026-10-30T14:00:00+09:00', 'patients': patients}
    validated = ChartBundle.model_validate(result).model_dump(mode='json')
    OUTPUT.write_text(json.dumps(validated, ensure_ascii=False, indent=2) + '\n')
    from backend.app import project_chart
    display = []
    for chart in ChartBundle.model_validate(validated).patients:
        row = project_chart(chart)
        row['medication_history'] = row['current_medications']
        row['current_medications'] = [m for m in row['medication_history'] if m['status_code'] == 'active']
        row['prescriptions'].sort(key=lambda r: r['date'], reverse=True)
        row['observations'].sort(key=lambda r: r['date'])
        row['encounters'].sort(key=lambda r: r['date'], reverse=True)
        display.append(row)
    (OUTPUT.parent / 'chart-display-v1.json').write_text(json.dumps(display, ensure_ascii=False, indent=2) + '\n')
    schemas = ROOT / 'backend/schemas'
    schemas.mkdir(exist_ok=True)
    (schemas / 'patient-chart-v1.schema.json').write_text(json.dumps(ChartBundle.model_json_schema(), ensure_ascii=False, indent=2) + '\n')
    template = copy.deepcopy(validated)
    template['patients'] = [copy.deepcopy(patients[7])]
    t = template['patients'][0]
    t['patient_id'] = 'demo-patient-099'
    t['chart_number'] = 'KJC-2026-099'
    t['identity'] = {'name': '가상환자', 'birth_date': '1950-01-15', 'sex': 'unknown', 'phone': '010-0000-1099', 'region': '가상 지역'}
    t['provenance']['verification_status'] = 'unverified'
    t['visit_preparation'] = {'mobility': 'unknown', 'hearing': 'unknown', 'preferred_channel': 'unknown', 'companion': 'unknown', 'phone_type': 'unknown', 'staff_note': ''}
    ChartBundle.model_validate(template)
    (schemas / 'patient-chart-v1.template.json').write_text(json.dumps(template, ensure_ascii=False, indent=2) + '\n')
    print(f"Validated {len(patients)} synthetic charts → {OUTPUT}")


if __name__ == '__main__':
    generate()
