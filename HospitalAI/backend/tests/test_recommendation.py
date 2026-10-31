from datetime import datetime, timezone
from app.recommendation import HospitalCandidate, RecommendationQuery, recommend


def hospital(**kwargs):
    base = dict(id='a', name='가상 병원', address='가상 주소', phone='01000000000',
                latitude=35.0, longitude=126.0, source='test')
    return HospitalCandidate(**(base | kwargs))


def test_ranking_returns_distance_specialty_and_opening_information():
    query = RecommendationQuery(latitude=35.0, longitude=126.0, specialty='내과')
    nearby = hospital(id='near', specialties=('내과',), specialists={'내과': 2}, open_status='open')
    far = hospital(id='far', latitude=35.1, specialties=('외과',), specialists={'외과': 3}, open_status='closed')
    result = recommend(query, [far, nearby])
    assert [row.id for row in result.hospitals] == ['near', 'far']
    assert result.hospitals[0].distance_km == 0
    assert result.hospitals[0].specialist_count == 2
    assert result.hospitals[0].specialty_match == 'yes'


def test_missing_data_remains_unknown_and_is_not_fabricated():
    query = RecommendationQuery(latitude=35.0, longitude=126.0, specialty='내과')
    result = recommend(query, [hospital()]).hospitals[0]
    assert result.specialist_count is None
    assert result.specialty_match == 'unknown'
    assert result.open_status == 'unknown'
    assert result.updated_at is None


def test_urgent_case_never_returns_routine_recommendations():
    result = recommend(RecommendationQuery(latitude=35.0, longitude=126.0, urgent=True), [hospital()])
    assert result.status == 'urgent'
    assert result.hospitals == ()


def test_straight_line_distance_is_ordered_accurately():
    query = RecommendationQuery(latitude=35.0, longitude=126.0)
    result = recommend(query, [hospital(id='near', latitude=35.01), hospital(id='far', latitude=35.1)])
    assert [row.id for row in result.hospitals] == ['near', 'far']
    assert 1.0 < result.hospitals[0].distance_km < 1.2


def test_absent_requested_specialty_does_not_infer_specialist_count():
    query = RecommendationQuery(latitude=35.0, longitude=126.0)
    result = recommend(query, [hospital(specialists={'내과': 5})]).hospitals[0]
    assert result.specialist_count is None
    assert result.specialty_match == 'unknown'


def test_registered_hours_use_korean_weekday_not_live_opening():
    query = RecommendationQuery(latitude=35.0, longitude=126.0, at=datetime(2026, 10, 26, 0, 30, tzinfo=timezone.utc))
    result = recommend(query, [hospital(hours={'mon': ('0900', '1800')})]).hospitals[0]
    assert result.registered_schedule_status == 'within_hours'
    assert result.open_status == 'unknown'


def test_overnight_previous_day_schedule_is_recognized():
    query = RecommendationQuery(latitude=35.0, longitude=126.0, at=datetime(2026, 10, 26, 16, 0, tzinfo=timezone.utc))
    result = recommend(query, [hospital(hours={'mon': ('2200', '0300')})]).hospitals[0]
    assert result.registered_schedule_status == 'within_hours'


def test_missing_hours_and_invalid_ranges_stay_unknown():
    query = RecommendationQuery(latitude=35.0, longitude=126.0, at=datetime(2026, 10, 26, 0, 30, tzinfo=timezone.utc))
    assert recommend(query, [hospital(hours={'mon': ('9900', '1800')})]).hospitals[0].registered_schedule_status == 'unknown'


def test_verified_matching_clinic_ranks_ahead_of_unknown_at_same_distance():
    query = RecommendationQuery(latitude=35, longitude=126, specialty='내과',
                                at=datetime(2026, 10, 26, 0, 30, tzinfo=timezone.utc))
    unknown = hospital(id='unknown')
    verified = hospital(id='verified', specialties=('내과',), specialists={'내과': 1}, hours={'mon': ('0900', '1800')})
    result = recommend(query, [unknown, verified])
    assert [entry.id for entry in result.hospitals] == ['verified', 'unknown']


def test_unknown_specialist_is_not_treated_as_known_absent():
    query = RecommendationQuery(latitude=35, longitude=126, specialty='내과')
    unknown = hospital(id='unknown', specialties=('내과',))
    absent = hospital(id='absent', specialties=('내과',), specialists={'내과': 0})
    result = recommend(query, [absent, unknown])
    assert [entry.id for entry in result.hospitals] == ['unknown', 'absent']
    assert result.hospitals[0].specialist_count is None


def test_verified_department_ranks_before_closer_unverified_or_mismatched():
    query = RecommendationQuery(latitude=35, longitude=126, specialty='내과')
    verified = hospital(id='verified', latitude=35.1, specialties=('내과',))
    unknown = hospital(id='unknown', latitude=35.001)
    mismatch = hospital(id='mismatch', latitude=35.002, specialties=('외과',))
    result = recommend(query, [mismatch, unknown, verified])
    assert [entry.id for entry in result.hospitals] == ['verified', 'unknown', 'mismatch']


def test_distance_precedes_specialists_and_registered_hours_among_matches():
    query = RecommendationQuery(latitude=35, longitude=126, specialty='내과',
                                at=datetime(2026, 10, 26, 0, 30, tzinfo=timezone.utc))
    nearer = hospital(id='nearer', latitude=35.01, specialties=('내과',), specialists={'내과': 0})
    farther = hospital(id='farther', latitude=35.02, specialties=('내과',),
                       specialists={'내과': 8}, hours={'mon': ('0900', '1800')})
    result = recommend(query, [farther, nearer])
    assert [entry.id for entry in result.hospitals] == ['nearer', 'farther']
    assert result.hospitals[0].score >= result.hospitals[1].score


def test_ranking_compares_unrounded_distances():
    query = RecommendationQuery(latitude=35, longitude=126, specialty='내과')
    nearer = hospital(id='z-nearer', latitude=35.00001, specialties=('내과',))
    farther = hospital(id='a-farther', latitude=35.00004, specialties=('내과',))
    result = recommend(query, [farther, nearer])
    assert [entry.id for entry in result.hospitals] == ['z-nearer', 'a-farther']
