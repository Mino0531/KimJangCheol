(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const paths = {
    building: 'M6 22V3h12v19 M3 22h18 M9 6h1 M14 6h1 M9 10h1 M14 10h1 M9 14h1 M14 14h1 M10 22v-4h4v4',
    phone: 'M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 2 .7 2.9a2 2 0 0 1-.5 2.1L8 10a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.5c.9.3 1.9.6 2.9.7a2 2 0 0 1 1.7 2z'
  };
  document.querySelectorAll('[data-icon]').forEach(el => {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.65');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', paths[el.dataset.icon] || '');
    svg.append(path);
    el.append(svg);
  });
  const access = $('#consultation-access');
  const form = $('#consultation-access-form');
  const input = $('#consultation-token');
  const lock = $('#consultation-lock');
  const status = $('#consultation-status');
  const results = $('#consultation-results');
  const count = $('#consultation-count');
  let accessToken = '';
  let generation = 0;
  let pending = false;
  const statuses = {completed:'생성 완료', degraded:'대체 안내', interrupted:'응답 중단', emergency:'긴급 안내', pending:'처리 대기', processing:'처리 중'};
  const schedules = {within_hours:'등록 요일 시간 내', outside_hours:'등록 요일 시간 외', unknown:'등록 진료시간 미확인'};
  const subjects = {yes:'진료과 등록 확인', no:'진료과 미등록', unknown:'진료과 미확인'};
  const days = {mon:'월',tue:'화',wed:'수',thu:'목',fri:'금',sat:'토',sun:'일'};
  const stamp = value => {
    const d = new Date(value);
    return !value || Number.isNaN(d.getTime()) ? '시각 미확인' : new Intl.DateTimeFormat('ko-KR', {timeZone:'Asia/Seoul',year:'numeric',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}).format(d);
  };
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  function addField(list, title, text, note) {
    const term = element('dt', title);
    if (note) term.append(' ', element('small', note));
    list.append(term, element('dd', text));
  }
  function describeRecommendation(rec) {
    const hospitals = Array.isArray(rec.hospitals) ? rec.hospitals : [];
    const lines = hospitals.map(h => {
      const distance = typeof h.distance_km === 'number' && Number.isFinite(h.distance_km) ? `직선거리 ${h.distance_km.toFixed(1)}km` : '거리 미확인';
      const specialists = Number.isInteger(h.specialist_count) && h.specialist_count >= 0 ? `등록 전문의 ${h.specialist_count}명` : '전문의 수 미확인';
      const specialty = rec.specialty ? `${rec.specialty} · ` : '';
      const schedule = schedules[h.registered_schedule_status] || schedules.unknown;
      const hours = h.hours && typeof h.hours === 'object' ? Object.entries(days).map(([key, label]) => {
        const range = h.hours[key];
        if (!Array.isArray(range) || range.length !== 2 || !range.every(v => typeof v === 'string' && /^\d{4}$/.test(v))) return '';
        return `${label} ${range[0].slice(0,2)}:${range[0].slice(2)}–${range[1].slice(0,2)}:${range[1].slice(2)}`;
      }).filter(Boolean).join(' · ') : '';
      return [`${h.name || '의료기관 미확인'} · ${distance}`, h.phone || '전화번호 미확인', `${specialty}${subjects[h.specialty_match] || subjects.unknown}`, specialists, schedule, hours ? `등록 시간 ${hours}` : '', `출처 ${h.source || '미확인'}`].filter(Boolean).join('\n');
    });
    return [lines.join('\n\n'), rec.warning || ''].filter(Boolean).join('\n\n');
  }
  function render(rows, recommendations) {
    count.textContent = `최근 ${rows.length}건`;
    const byCall = new Map();
    for (const rec of recommendations) if (!byCall.has(rec.call_id)) byCall.set(rec.call_id, rec);
    const fragment = document.createDocumentFragment();
    for (const row of rows) {
      const article = element('article', null, 'consultation-record');
      const header = element('header');
      const heading = element('div');
      heading.append(element('strong', row.phone || '발신번호 미확인'), element('span', stamp(row.occurred_at)));
      const interruptedBeforeCompletion = typeof row.event_id === 'string' && row.event_id.startsWith('turn:') && rows.some(other => other.call_id === row.call_id && other.source === row.source && other.event_id === `interrupted:${row.event_id.slice('turn:'.length)}`);
      header.append(heading, element('span', statuses[interruptedBeforeCompletion ? 'interrupted' : row.answer_status] || '상태 미확인', 'tag violet'));
      const list = element('dl');
      addField(list, '사용자 문의', row.user_text || '내용 없음');
      if (interruptedBeforeCompletion) addField(list, '안내 상태', '안내가 중단되어 끝까지 전달됐는지 확인되지 않았습니다.');
      addField(list, row.answer_status === 'interrupted' ? '안내 상태' : 'AI 생성 답변', row.assistant_text || '답변 없음', row.answer_status === 'interrupted' ? '' : '발화 여부 미확인');
      const rec = byCall.get(row.call_id);
      if (rec) {
        addField(list, '추천 참고자료', describeRecommendation(rec), '실제 진료·전문의 근무 확인 필요');
        byCall.delete(row.call_id);
      }
      article.append(header, list);
      const references = Array.isArray(row.sources) ? row.sources.map(source => source.source || source.id || '').filter(Boolean).join(' · ') : '';
      if (references) article.append(element('p', `참고 자료: ${references}`, 'consultation-source'));
      fragment.append(article);
    }
    for (const rec of byCall.values()) {
      const article = element('article', null, 'consultation-record');
      const header = element('header');
      const heading = element('div');
      heading.append(element('strong', '상담 기록 연동 대기'), element('span', stamp(rec.occurred_at)));
      header.append(heading);
      const list = element('dl');
      addField(list, '추천 참고자료', describeRecommendation(rec), '실제 진료·전문의 근무 확인 필요');
      article.append(header, list);
      fragment.append(article);
    }
    results.replaceChildren(fragment);
    if (!results.childElementCount) results.append(element('div', '전달된 상담 결과가 없습니다.', 'empty-state'));
  }
  async function refresh(token) {
    if (pending) return false;
    const started = generation;
    pending = true;
    status.textContent = '상담 결과를 불러오는 중입니다.';
    try {
      const response = await fetch('/api/team/consultation-results', {headers:{Authorization:`Bearer ${token}`},cache:'no-store'});
      if (response.status === 401) {
        generation++;
        accessToken = '';
        input.value = '';
        results.replaceChildren();
        count.textContent = '잠김';
        access.hidden = false;
        lock.hidden = true;
        status.textContent = '접근 토큰을 확인해 주세요.';
        return false;
      }
      if (!response.ok) throw new Error('request failed');
      const rows = await response.json();
      if (!Array.isArray(rows)) throw new Error('invalid response');
      if (started !== generation) return false;
      let recommendations = [];
      let recommendationError = false;
      try {
        const recResponse = await fetch('/api/team/recommendation-results', {headers:{Authorization:`Bearer ${token}`},cache:'no-store'});
        if (!recResponse.ok) throw new Error('request failed');
        recommendations = await recResponse.json();
        if (!Array.isArray(recommendations)) throw new Error('invalid response');
      } catch { recommendations = []; recommendationError = true; }
      if (started !== generation) return false;
      render(rows, recommendations);
      status.textContent = recommendationError ? '병원 추천 결과를 불러오지 못했습니다.' : '';
      return true;
    } catch {
      if (started === generation) status.textContent = '상담 결과를 불러오지 못했습니다. 접근 토큰과 서버 상태를 확인해 주세요.';
      return false;
    } finally { pending = false; }
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const token = input.value.trim();
    if (!token) return;
    const button = form.querySelector('button');
    button.disabled = true;
    try {
      if (await refresh(token)) {
        accessToken = token;
        input.value = '';
        access.hidden = true;
        lock.hidden = false;
      }
    } finally { button.disabled = false; }
  });
  lock.addEventListener('click', () => {
    generation++;
    accessToken = '';
    input.value = '';
    results.replaceChildren();
    count.textContent = '잠김';
    status.textContent = '';
    access.hidden = false;
    lock.hidden = true;
    input.focus();
  });
  setInterval(() => { if (accessToken && !document.hidden) void refresh(accessToken); }, 5000);
})();
