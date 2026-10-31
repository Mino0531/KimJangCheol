(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text != null) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const icons = {
    grid: 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
    phone: 'M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 2 .7 2.9a2 2 0 0 1-.5 2.1L8 10a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.5c.9.3 1.9.6 2.9.7a2 2 0 0 1 1.7 2z',
    pin: 'M20 10c0 6-8 12-8 12S4 16 4 10a8 8 0 1 1 16 0z M15 10a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
    note: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z M14 2v6h6 M8 13h8 M8 17h6',
    building: 'M6 22V3h12v19 M3 22h18 M9 6h1 M14 6h1 M9 10h1 M14 10h1 M10 22v-4h4v4',
    home: 'm3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z',
    bell: 'M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9 M10 21h4',
    menu: 'M3 6h18 M3 12h18 M3 18h18',
    search: 'M21 21l-5-5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
    'chevron-left': 'm15 5-7 7 7 7', 'chevron-right': 'm9 5 7 7-7 7',
    activity: 'M2 12h4l3-8 6 16 3-8h4', 'check-circle': 'M22 11v1a10 10 0 1 1-6-9 M22 4 12 14l-3-3',
    inbox: 'M4 3h16l2 12v6H2v-6z M2 15h6l2 3h4l2-3h6',
    users: 'M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2 M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
  };
  function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.65');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', icons[name] || icons.note);
    svg.append(path);
    return svg;
  }
  $$('[data-icon]').forEach(element => element.append(icon(element.dataset.icon)));
  for (const link of $$('.sidebar a.nav-item')) {
    for (const child of link.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) child.nodeValue = child.nodeValue.replace('예시 자료', '예시\u00a0자료').replace('지자체 대시보드 ↗', '지자체\u00a0대시보드\u00a0↗');
    }
  }
  $('.workspace strong').textContent = '방문 준비';
  $('.workspace div > span').textContent = '원무팀';
  $('.demo-help p').textContent = '인계 상담 확인';
  $('#page-description').textContent = '지자체 인계 상담을 확인하세요.';

  const form = $('#consultation-access-form');
  const input = $('#consultation-token');
  const access = $('#consultation-access');
  const lock = $('#consultation-lock');
  const status = $('#consultation-status');
  const count = $('#consultation-count');
  const results = $('#consultation-results');
  const table = $('#call-table');
  const detail = $('#detail-panel');
  const notification = $('#notification-panel');
  const state = {token: '', rows: [], playback: new Set(), filter: 'all', search: '', page: 0, selected: null, generation: 0, pending: false, saving: false, view: 'dashboard'};
  const pageSize = 7;
  const labels = {completed: '답변 완료', degraded: '대체 안내', interrupted: '응답 중단', emergency: '긴급 안내', unanswered: '안내 미확인', redacted: '원문 확인 제한'};
  const badgeClass = {completed: 'green', degraded: 'amber', interrupted: 'amber', emergency: 'amber', unanswered: 'amber'};
  const datetime = value => {
    const date = new Date(value);
    return value && !Number.isNaN(date.getTime())
      ? new Intl.DateTimeFormat('ko-KR', {timeZone: 'Asia/Seoul', year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'}).format(date)
      : '시각 미확인';
  };
  const badge = value => node('span', labels[value] || '상태 미확인', `tag ${badgeClass[value] || 'neutral'}`);
  const textOr = (value, fallback) => typeof value === 'string' && value.trim() ? value.trim() : fallback;
  const rowKey = row => String(row.call_id);
  const eventKey = row => JSON.stringify([row.source, row.event_id]);
  const workflowLabels = {received: '수신', in_progress: '검토 중', completed: '처리 완료'};
  const isInterruptedAnswer = row => typeof row.event_id === 'string' && row.event_id.startsWith('turn:') && state.rows.some(group => group.call_id === row.call_id && group.events.some(entry => entry.source === row.source && entry.event_id === `interrupted:${row.event_id.slice('turn:'.length)}`));
  const displayStatus = row => isInterruptedAnswer(row) ? 'interrupted' : row.answer_status;
  const playbackStatus = row => displayStatus(row) === 'interrupted' ? '안내가 중단되어 끝까지 전달됐는지 확인되지 않았습니다.' : typeof row.event_id === 'string' && row.event_id.startsWith('turn:') && state.playback.has(JSON.stringify([row.call_id, `playback:${row.event_id.slice('turn:'.length)}`])) ? '재생 완료 · 청취 여부 미확인' : '재생 여부 미확인';
  function showUnlinked() {
    $('#map-canvas').replaceChildren(node('div', '현재 위치 정보가 연결되지 않았습니다.', 'empty-state'));
    $('#map-count').textContent = '위치 미연결';
    const notes = state.rows.filter(row => row.workflow?.note);
    const activities = state.rows.filter(row => row.workflow?.updated_at);
    $('#activity-list').replaceChildren(...(activities.length ? activities.slice(0, 5).map(row => node('div', `${workflowLabels[row.workflow.status] || '상태 확인'} · ${datetime(row.workflow.updated_at)}`, 'workflow-activity')) : [node('div', '직원 처리 기록이 없습니다.', 'empty-state')]));
    $('#all-notes-list').replaceChildren(...(notes.length ? notes.map(row => node('div', `${textOr(row.phone, '번호 미확인')} · ${row.workflow.note}`, 'workflow-activity')) : [node('div', '직원 메모가 없습니다.', 'empty-state')]));
    $('#all-notes-count').textContent = state.token ? String(notes.length) : '–';
    $('#nav-note-count').textContent = state.token ? String(notes.length) : '–';
  }
  function renderStats() {
    const values = [
      ['인계 상담', state.rows.length, 'phone', 'violet', '통화별 상담'],
      ['긴급 안내', state.rows.filter(row => row.answer_status === 'emergency').length, 'activity', 'amber', '의료진 확인 필요'],
      ['답변 완료', state.rows.filter(row => displayStatus(row) === 'completed').length, 'check-circle', 'green', 'AI 응답 생성'],
      ['대체 안내', state.rows.filter(row => row.answer_status === 'degraded').length, 'inbox', 'violet', '추가 확인 필요'],
    ];
    const stats = $('#stats');
    stats.replaceChildren();
    for (const [title, number, graphic, color, explanation] of values) {
      const article = node('article', null, 'stat');
      const head = node('div', null, 'stat-head');
      const symbol = node('span', null, `stat-icon ${color}`);
      symbol.append(icon(graphic));
      head.append(node('span', title), symbol);
      const total = node('div', null, 'stat-number');
      total.append(document.createTextNode(state.token ? String(number) : '–'), node('small', '건'));
      const bottom = node('div', null, 'stat-bottom');
      bottom.append(node('span', state.token ? explanation : '인증 후 확인'));
      article.append(head, total, bottom);
      stats.append(article);
    }
    $('#nav-call-count').textContent = state.token ? String(state.rows.length) : '–';
    const urgentCount = state.rows.filter(row => row.answer_status === 'emergency' && row.workflow?.status !== 'completed').length;
    $('#notification-dot').hidden = !state.token || urgentCount === 0;
  }
  function filtered() {
    const query = state.search.toLocaleLowerCase();
    return state.rows.filter(row => {
      if (state.filter !== 'all' && !row.events.some(entry => displayStatus(entry) === state.filter)) return false;
      if (!query) return true;
      return [row.phone, ...row.events.flatMap(entry => [entry.user_text, entry.assistant_text]), ...row.unansweredUtterances.map(item => item.user_text)].some(value => String(value || '').toLocaleLowerCase().includes(query));
    });
  }
  function cell(text, className) { return node('td', text, className); }
  function renderTable() {
    const rows = filtered();
    state.page = Math.max(0, Math.min(state.page, Math.ceil(rows.length / pageSize) - 1));
    const page = rows.slice(state.page * pageSize, (state.page + 1) * pageSize);
    const title = $('#list-title');
    title.replaceChildren(document.createTextNode('인계된 상담 '), node('span', state.token ? String(rows.length) : '잠김', 'count-badge'));
    $('#list-description').textContent = '직원이 지자체에서 인계한 상담 기록';
    table.replaceChildren();
    for (const row of page) {
      const tr = node('tr', null, state.selected === rowKey(row) ? 'selected' : '');
      tr.tabIndex = 0;
      tr.setAttribute('role', 'button');
      tr.setAttribute('aria-label', `${textOr(row.phone, '발신번호 미확인')} 상담 상세 보기`);
      tr.setAttribute('aria-selected', String(state.selected === rowKey(row)));
      const person = node('td');
      const wrapper = node('div', null, 'person-cell');
      const personInfo = node('span');
      personInfo.append(node('strong', '상담자 미확인'));
      if (row.answer_status === 'emergency') personInfo.append(node('small', '긴급 안내', 'amber-text'));
      if (displayStatus(row) === 'interrupted') personInfo.append(node('small', '응답 중단', 'amber-text'));
      wrapper.append(node('span', '상', 'person-avatar violet'), personInfo);
      person.append(wrapper);
      const purpose = cell(textOr(row.user_text, '문의 내용 없음'), 'purpose-cell');
      const kind = node('td');
      kind.append(badge(displayStatus(row)));
      const arrow = node('td');
      arrow.append(icon('chevron-right'));
      tr.append(person, cell(textOr(row.phone, '번호 미확인'), 'contact-cell'), purpose,
        cell(datetime(row.occurred_at), 'time-cell'), kind, cell(workflowLabels[row.workflow?.status] || '수신'), arrow);
      const select = () => { state.selected = rowKey(row); renderTable(); renderDetail(); };
      tr.addEventListener('click', select);
      tr.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(); } });
      table.append(tr);
    }
    if (!page.length) {
      const tr = node('tr', null, 'empty-row');
      const td = node('td'); td.colSpan = 7;
      td.append(node('div', state.token ? '조건에 맞는 인계 상담이 없습니다.' : '병원 담당자 인증 후 인계 상담을 확인할 수 있습니다.', 'empty-state'));
      tr.append(td); table.append(tr);
    }
    $('#page-range').textContent = rows.length ? `${state.page * pageSize + 1}–${Math.min((state.page + 1) * pageSize, rows.length)} / ${rows.length}건` : '0건';
    $('#prev-page').disabled = state.page === 0;
    $('#next-page').disabled = (state.page + 1) * pageSize >= rows.length;
    $$('#status-tabs [data-filter]').forEach(button => {
      const selected = button.dataset.filter === state.filter;
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
  }
  function field(parent, title, value) {
    const block = node('section', null, 'detail-fact');
    block.append(node('label', title), node('p', textOr(value, '미확인')));
    parent.append(block);
  }
  function workflowForm(row) {
    const workflow = row.workflow || {version: 0, status: 'received'};
    const section = node('section', null, 'workflow-section');
    section.append(node('h3', '병원 처리'), node('p', `현재 상태: ${workflowLabels[workflow.status] || '수신'}`));
    if (workflow.actor_name) section.append(node('p', `담당자: ${workflow.actor_name}`));
    if (workflow.note) section.append(node('p', `처리 메모: ${workflow.note}`));
    if (workflow.updated_at) section.append(node('p', `처리 시각: ${datetime(workflow.updated_at)}`));
    if (workflow.status === 'completed') return section;
    const form = node('form', null, 'workflow-form');
    const nameLabel = node('label', '담당자 이름');
    const name = document.createElement('input');
    name.name = 'actor_name'; name.required = true; name.maxLength = 100; name.autocomplete = 'name';
    name.value = workflow.actor_name || '';
    nameLabel.append(name);
    const noteLabel = node('label', '처리 메모 (선택)');
    const note = document.createElement('textarea');
    note.name = 'note'; note.maxLength = 2000; note.rows = 3;
    noteLabel.append(note);
    const feedback = node('p', '', 'workflow-feedback');
    feedback.setAttribute('role', 'status');
    const button = node('button', workflow.status === 'in_progress' ? '처리 완료' : '검토 시작', 'button primary');
    button.type = 'submit';
    form.append(nameLabel, noteLabel, button, feedback);
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (state.saving) return;
      const actor = name.value.trim();
      if (!actor) { feedback.textContent = '담당자 이름을 입력해 주세요.'; name.focus(); return; }
      // Ignore an older polling response that could otherwise overwrite this update.
      const generation = ++state.generation;
      state.saving = true; button.disabled = true; feedback.textContent = '처리 내용을 저장하고 있습니다.';
      try {
        const response = await fetch(`/api/team/municipality-cases/${encodeURIComponent(row.call_id)}/workflow`, {
          method: 'POST', headers: {Authorization: `Bearer ${state.token}`, 'Content-Type': 'application/json'},
          body: JSON.stringify({expected_version: workflow.version, action: workflow.status === 'in_progress' ? 'complete' : 'start', actor_name: actor, note: note.value.trim()}),
        });
        if (generation !== state.generation) return;
        if (response.status === 401 || response.status === 403) { clearSession('접근 토큰을 확인해 주세요.'); return; }
        if (response.status === 409) {
          feedback.textContent = '다른 직원이 변경했습니다. 최신 내용을 다시 확인해 주세요.';
          const reload = node('button', '최신 상태 보기', 'button quiet');
          reload.type = 'button'; reload.addEventListener('click', () => { void refresh(state.token); });
          feedback.append(document.createTextNode(' '), reload);
          return;
        }
        if (!response.ok) throw new Error('workflow_failed');
        const result = await response.json();
        if (!result.workflow || typeof result.workflow.version !== 'number') throw new Error('workflow_invalid');
        row.workflow = result.workflow;
        render();
      } catch {
        if (generation === state.generation) feedback.textContent = '저장하지 못했습니다. 연결을 확인하고 다시 시도해 주세요.';
      } finally { state.saving = false; button.disabled = false; }
    });
    section.append(form);
    return section;
  }
  function renderDetail() {
    const row = state.rows.find(item => rowKey(item) === state.selected);
    detail.replaceChildren();
    if (!row) {
      detail.append(node('div', state.token ? '목록에서 상담을 선택해 주세요.' : '인증 후 상담을 선택해 주세요.', 'empty-state'));
      return;
    }
    const heading = node('div', null, 'detail-heading');
    heading.append(node('h2', '인계 상담 상세'), badge(displayStatus(row)));
    const person = node('div', null, 'detail-person');
    const copy = node('div');
    copy.append(node('h3', '상담자 정보 미확인'), node('p', textOr(row.phone, '연락처 미확인')));
    person.append(node('span', '상', 'detail-avatar'), copy);
    const body = node('div', null, 'detail-tab-content');
    const digest = node('section'); body.append(digest);
    window.ConsultationDigest?.mount(digest, {scope:'hospital', callId:row.call_id,
      version:JSON.stringify([...row.events, ...row.unansweredUtterances].map(entry => entry.user_text))});
    field(body, '상담 시각', datetime(row.occurred_at));
    field(body, '병원 인계 시각', datetime(row.handed_at));
    if (row.callEnd) {
      const reasons = {caller: '주민이 상담을 마침', hangup: '전화 종료', connection_lost: '연결 끊김', safety_stop: '안전상 종료', consent_declined: '동의 거부'};
      field(body, '통화 종료', `${reasons[row.callEnd.end_reason] || '종료 사유 미확인'} · ${datetime(row.callEnd.occurred_at)}`);
    }
    for (const entry of row.events) {
      field(body, '사용자 문의', entry.user_text);
      if (entry.answer_status !== 'interrupted' && entry.answer_status !== 'emergency') field(body, '재생 상태', playbackStatus(entry));
      field(body, entry.answer_status === 'interrupted' ? '안내 상태' : 'AI 생성 답변', entry.assistant_text);
    }
    for (const utterance of row.unansweredUtterances) {
      field(body, '사용자 문의 · 안내 여부 미확인', utterance.user_text);
    }
    for (const redaction of row.privacyRedactions) {
      field(body, '원문 확인 제한', `위치 동의 철회로 일부 내용을 확인할 수 없습니다.\n${datetime(redaction.occurred_at)}`);
    }
    for (const recommendation of row.recommendations) {
      const hospitals = Array.isArray(recommendation.hospitals) ? recommendation.hospitals : [];
      const details = hospitals.filter(hospital => hospital && typeof hospital === 'object').map(hospital => {
        const parts = [textOr(hospital.name, '병원 이름 미확인')];
        if (hospital.phone) parts.push(`전화 ${hospital.phone}`);
        if (hospital.address) parts.push(hospital.address);
        if (typeof hospital.distance_km === 'number' && Number.isFinite(hospital.distance_km)) parts.push(`직선거리\u00a0${hospital.distance_km}km`);
        if (hospital.specialty_match === 'yes') parts.push('요청\u00a0진료과\u00a0등록');
        else if (hospital.specialty_match === 'no') parts.push('요청\u00a0진료과\u00a0미등록');
        else if (hospital.specialty_match === 'unknown') parts.push('진료과\u00a0미확인');
        if (typeof hospital.specialist_count === 'number' && Number.isFinite(hospital.specialist_count)) parts.push(`등록\u00a0전문의\u00a0${hospital.specialist_count}명`);
        if (hospital.registered_schedule_status === 'within_hours') parts.push('등록\u00a0진료시간\u00a0내');
        else if (hospital.registered_schedule_status === 'outside_hours') parts.push('등록\u00a0진료시간\u00a0외');
        else parts.push('진료시간\u00a0미확인');
        return parts.join(' · ');
      });
      if (recommendation.specialty) details.unshift(`희망 진료과: ${recommendation.specialty}`);
      if (recommendation.warning) details.push(recommendation.warning);
      field(body, '추천 병원 정보', details.join('\n') || '추천 정보 미확인');
    }
    field(body, '환자 차트', '기존 의료기록과 연결되지 않았습니다.');
    field(body, '현재 위치', '위치 정보가 연결되지 않았습니다.');
    body.append(workflowForm(row));
    detail.append(heading, person, body, node('div', 'AI 답변은 의료진이 확인해 주세요.', 'detail-footer'));
  }
  function renderNotifications() {
    const urgent = state.rows.filter(row => row.answer_status === 'emergency' && row.workflow?.status !== 'completed');
    notification.replaceChildren(node('strong', state.token ? `긴급 안내 ${urgent.length}건` : '인증 후 확인 가능합니다.'));
    for (const row of urgent.slice(0, 5)) {
      const button = node('button', null, 'notification-row');
      const text = node('span');
      text.append(node('strong', textOr(row.phone, '발신번호 미확인')), node('small', datetime(row.occurred_at)));
      button.append(text, badge('emergency'));
      button.addEventListener('click', () => {
        state.selected = rowKey(row); state.filter = 'all'; state.search = ''; $('#search-input').value = '';
        setView('dashboard'); renderTable(); renderDetail();
        notification.hidden = true; $('#notifications-button').setAttribute('aria-expanded', 'false');
      });
      notification.append(button);
    }
    if (state.token && !urgent.length) notification.append(node('div', '긴급 안내가 없습니다.', 'empty-state'));
  }
  function render() {
    renderStats(); renderTable(); renderDetail(); renderNotifications(); showUnlinked();
    count.textContent = state.token ? `${state.rows.length}건` : '잠김';
    results.replaceChildren(node('p', state.token ? '아래 목록에서 인계된 상담을 선택하세요.' : '', 'consultation-note'));
  }
  function clearSession(message = '') {
    state.generation += 1; state.token = ''; state.rows = []; state.playback = new Set(); state.selected = null;
    state.search = ''; state.filter = 'all'; state.page = 0;
    $('#search-input').value = ''; notification.hidden = true;
    $('#notifications-button').setAttribute('aria-expanded', 'false');
    input.value = ''; access.hidden = false; lock.hidden = true;
    status.textContent = message; render();
  }
  async function refresh(token) {
    if (state.pending) return false;
    const generation = state.generation;
    state.pending = true;
    status.textContent = '인계된 상담을 불러오는 중입니다.';
    try {
      const response = await fetch('/api/team/municipality-handoffs', {headers: {Authorization: `Bearer ${token}`}, cache: 'no-store'});
      if (response.status === 401 || response.status === 403) {
        if (generation === state.generation) clearSession('접근 토큰을 확인해 주세요.');
        return false;
      }
      if (!response.ok) throw new Error('상담 수신함을 불러오지 못했습니다.');
      const data = await response.json();
      if (!Array.isArray(data.handoffs)) throw new Error('상담 응답 형식을 확인할 수 없습니다.');
      if (generation !== state.generation) return false;
      const unique = new Map();
      const workflows = new Map();
      const callEnds = new Map();
      const recommendations = new Map();
      const unanswered = new Map();
      const redactions = new Map();
      const handoffTimes = new Map();
      const playback = new Set();
      for (const handoff of data.handoffs) {
        if (!handoff || !Array.isArray(handoff.events)) continue;
        if (typeof handoff.call_id === 'string' && Array.isArray(handoff.privacy_redactions)) {
          if (!redactions.has(handoff.call_id)) redactions.set(handoff.call_id, new Map());
          for (const item of handoff.privacy_redactions) {
            if (item && typeof item.event_id === 'string') redactions.get(handoff.call_id).set(item.event_id, item);
          }
        }
        if (typeof handoff.call_id === 'string' && String(handoff.handed_at) > (handoffTimes.get(handoff.call_id) || '')) handoffTimes.set(handoff.call_id, String(handoff.handed_at));
        if (typeof handoff.call_id === 'string' && Array.isArray(handoff.unanswered_utterances)) {
          if (!unanswered.has(handoff.call_id)) unanswered.set(handoff.call_id, new Map());
          for (const item of handoff.unanswered_utterances) {
            if (item && typeof item.utterance_id === 'string' && typeof item.user_text === 'string') unanswered.get(handoff.call_id).set(item.utterance_id, item);
          }
        }
        if (typeof handoff.call_id === 'string' && Array.isArray(handoff.recommendations)) {
          if (!recommendations.has(handoff.call_id)) recommendations.set(handoff.call_id, new Map());
          for (const item of handoff.recommendations) {
            if (item && typeof item.event_id === 'string') recommendations.get(handoff.call_id).set(item.event_id, item);
          }
        }
        if (typeof handoff.call_id === 'string') {
          const previous = callEnds.get(handoff.call_id);
          if (!previous || String(handoff.handed_at) > previous.handed_at) callEnds.set(handoff.call_id, {handed_at: String(handoff.handed_at), value: handoff.call_end || null});
        }
        if (typeof handoff.call_id === 'string' && handoff.workflow && typeof handoff.workflow.version === 'number') {
          const previous = workflows.get(handoff.call_id);
          if (!previous || handoff.workflow.version > previous.version) workflows.set(handoff.call_id, handoff.workflow);
        }
        if (typeof handoff.call_id === 'string' && Array.isArray(handoff.playback_completed_event_ids)) {
          for (const id of handoff.playback_completed_event_ids) if (typeof id === 'string' && id.startsWith('playback:')) playback.add(JSON.stringify([handoff.call_id, id]));
        }
        for (const row of handoff.events) {
          if (!row || typeof row !== 'object' || typeof row.event_id !== 'string' || typeof row.source !== 'string') continue;
          const previous = unique.get(eventKey(row));
          if (!previous || String(handoff.handed_at) > String(previous.handed_at)) unique.set(eventKey(row), {...row, call_id: handoff.call_id, handed_at: handoff.handed_at});
        }
      }
      const groups = new Map();
      for (const entry of unique.values()) {
        if (typeof entry.call_id !== 'string') continue;
        if (!groups.has(entry.call_id)) groups.set(entry.call_id, []);
        groups.get(entry.call_id).push(entry);
      }
      for (const [callId, items] of unanswered) if (items.size && !groups.has(callId)) groups.set(callId, []);
      for (const [callId, items] of redactions) if (items.size && !groups.has(callId)) groups.set(callId, []);
      state.rows = [...groups.entries()].map(([callId, entries]) => {
        entries.sort((a, b) => String(a.occurred_at).localeCompare(String(b.occurred_at)));
        const answeredIds = new Set(entries.map(entry => entry.utterance_id).filter(id => typeof id === 'string'));
        const unansweredUtterances = [...(unanswered.get(callId)?.values() || [])].filter(item => !answeredIds.has(item.utterance_id));
        const privacyRedactions = [...(redactions.get(callId)?.values() || [])];
        const lastUnanswered = unansweredUtterances[unansweredUtterances.length - 1];
        const lastRedaction = privacyRedactions[privacyRedactions.length - 1];
        const representative = entries.find(entry => entry.answer_status === 'emergency') || entries[entries.length - 1] || {
          ...(lastUnanswered || lastRedaction), user_text: lastUnanswered?.user_text || '원문 확인 제한',
          answer_status: lastUnanswered ? 'unanswered' : 'redacted', source: 'hospital-ai', assistant_text: ''};
        const handedAt = handoffTimes.get(callId) || '';
        return {...representative, call_id: callId, handed_at: handedAt, events: entries,
          workflow: workflows.get(callId) || {version: 0, status: 'received'}, callEnd: callEnds.get(callId)?.value || null,
          recommendations: [...(recommendations.get(callId)?.values() || [])], unansweredUtterances, privacyRedactions};
      }).sort((a, b) => String(b.handed_at).localeCompare(String(a.handed_at)));
      state.playback = playback;
      state.token = token;
      if (!state.rows.some(row => rowKey(row) === state.selected)) state.selected = state.rows.length ? rowKey(state.rows[0]) : null;
      access.hidden = true; lock.hidden = false; input.value = ''; status.textContent = '';
      render();
      return true;
    } catch {
      if (generation === state.generation) status.textContent = '상담 수신함을 불러오지 못했습니다. 서버 상태를 확인해 주세요.';
      return false;
    } finally { state.pending = false; }
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const token = input.value.trim();
    if (!token) return;
    const submit = form.querySelector('button'); submit.disabled = true;
    try { await refresh(token); } finally { submit.disabled = false; }
  });
  lock.addEventListener('click', () => { clearSession(); input.focus(); });
  $('#status-tabs').addEventListener('click', event => {
    const button = event.target.closest('[data-filter]');
    if (button) { state.filter = button.dataset.filter; state.page = 0; renderTable(); }
  });
  $('#search-input').addEventListener('input', event => { state.search = event.target.value.trim(); state.page = 0; renderTable(); });
  $('#prev-page').addEventListener('click', () => { state.page -= 1; renderTable(); });
  $('#next-page').addEventListener('click', () => { state.page += 1; renderTable(); });
  $('#export-button').addEventListener('click', () => {
    if (!state.token || !state.rows.length) {
      status.textContent = '내보낼 인계 상담이 없습니다.';
      return;
    }
    const quote = value => `"${String(value ?? '').replace(/^[\s\u0000-\u001f]*[=+@-]/, "'$&").replace(/"/g, '""')}"`;
    const entries = [['상담 시각', '연락처', '사용자 문의', 'AI 생성 답변', '안내 상태', '재생 상태'],
      ...filtered().flatMap(row => [
        ...row.events.map(entry => [datetime(entry.occurred_at), entry.phone, entry.user_text, entry.assistant_text, labels[displayStatus(entry)] || '상태 미확인', playbackStatus(entry)]),
        ...row.unansweredUtterances.map(entry => [datetime(entry.occurred_at), entry.phone || row.phone, entry.user_text, '', '안내 여부 미확인', '재생 여부 미확인']),
        ...row.privacyRedactions.map(entry => [datetime(entry.occurred_at), entry.phone || row.phone, '원문 확인 제한', '', '원문 확인 제한', '']),
      ])];
    const url = URL.createObjectURL(new Blob(['\ufeff' + entries.map(row => row.map(quote).join(',')).join('\r\n')], {type: 'text/csv;charset=utf-8'}));
    const link = document.createElement('a'); link.href = url; link.download = '병원_인계_상담.csv'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  function setView(view) {
    state.view = view;
    $$('.nav-item[data-view]').forEach(button => {
      const selected = button.dataset.view === view;
      button.classList.toggle('active', selected);
      if (selected) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
    });
    const captions = {
      dashboard: ['대시보드', '방문 준비 데스크', '지자체 인계 상담을 확인하세요.'],
      history: ['통화 기록', '인계된 상담 기록', '병원 인계 상담을 확인하세요.'],
      locations: ['위치 현황', '위치 현황', '현재 위치 정보는 연결되지 않았습니다.'],
      notes: ['직원 메모', '직원 메모', '병원 처리 메모를 확인하세요.'],
    };
    const [name, title, description] = captions[view] || captions.dashboard;
    $('#breadcrumb-current').textContent = name;
    $('#page-title').textContent = title;
    $('#page-description').textContent = description;
    document.body.classList.toggle('locations-view', view === 'locations');
    document.body.classList.toggle('notes-view', view === 'notes');
    $('#all-notes-panel').hidden = view !== 'notes';
    $('#sidebar').classList.remove('open'); $('#menu-toggle').setAttribute('aria-expanded', 'false');
  }
  $$('.nav-item[data-view]').forEach(button => button.addEventListener('click', () => setView(button.dataset.view)));
  $('#menu-toggle').addEventListener('click', () => { const open = $('#sidebar').classList.toggle('open'); $('#menu-toggle').setAttribute('aria-expanded', String(open)); });
  $('#notifications-button').addEventListener('click', () => {
    notification.hidden = !notification.hidden;
    $('#notifications-button').setAttribute('aria-expanded', String(!notification.hidden));
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') { notification.hidden = true; $('#notifications-button').setAttribute('aria-expanded', 'false'); $('#sidebar').classList.remove('open'); $('#menu-toggle').setAttribute('aria-expanded', 'false'); }
  });
  $('#today-label').textContent = new Intl.DateTimeFormat('ko-KR', {timeZone: 'Asia/Seoul', year: 'numeric', month: 'long', day: 'numeric', weekday: 'short'}).format(new Date());
  render();
  setInterval(() => { if (state.token && !document.hidden && !state.saving && !document.activeElement?.closest('.workflow-form')) void refresh(state.token); }, 5000);
})();
