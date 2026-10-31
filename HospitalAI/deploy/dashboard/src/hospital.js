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

  const status = $('#consultation-status');
  const retry = $('#consultation-retry');
  const count = $('#consultation-count');
  const results = $('#consultation-results');
  const table = $('#call-table');
  const detail = $('#detail-panel');
  const notification = $('#notification-panel');
  const state = {connected: false, authBlocked: false, rows: [], playback: new Set(), filter: 'all', search: '', page: 0, selected: null, generation: 0, pending: false, view: 'dashboard'};
  const pageSize = 7;
  const labels = {completed: '답변 완료', degraded: '대체 안내', interrupted: '응답 중단', emergency: '긴급 안내'};
  const badgeClass = {completed: 'green', degraded: 'amber', interrupted: 'amber', emergency: 'amber'};
  const datetime = value => {
    const date = new Date(value);
    return value && !Number.isNaN(date.getTime())
      ? new Intl.DateTimeFormat('ko-KR', {timeZone: 'Asia/Seoul', year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'}).format(date)
      : '시각 미확인';
  };
  const badge = value => node('span', labels[value] || '상태 미확인', `tag ${badgeClass[value] || 'neutral'}`);
  const textOr = (value, fallback) => typeof value === 'string' && value.trim() ? value.trim() : fallback;
  const rowKey = row => JSON.stringify([row.source, row.event_id]);
  const isInterruptedAnswer = row => typeof row.event_id === 'string' && row.event_id.startsWith('turn:') && state.rows.some(entry => entry.call_id === row.call_id && entry.source === row.source && entry.event_id === `interrupted:${row.event_id.slice('turn:'.length)}`);
  const displayStatus = row => isInterruptedAnswer(row) ? 'interrupted' : row.answer_status;
  const playbackStatus = row => displayStatus(row) === 'interrupted' ? '안내가 중단되어 끝까지 전달됐는지 확인되지 않았습니다.' : typeof row.event_id === 'string' && row.event_id.startsWith('turn:') && state.playback.has(JSON.stringify([row.call_id, `playback:${row.event_id.slice('turn:'.length)}`])) ? '재생 완료 · 청취 여부 미확인' : '재생 여부 미확인';
  function showUnlinked() {
    $('#map-canvas').replaceChildren(node('div', '현재 위치 정보가 연결되지 않았습니다.', 'empty-state'));
    $('#map-count').textContent = '위치 미연결';
    $('#activity-list').replaceChildren(node('div', '직원 처리 기록이 연결되지 않았습니다.', 'empty-state'));
    $('#all-notes-list').replaceChildren(node('div', '직원 메모가 연결되지 않았습니다.', 'empty-state'));
    $('#all-notes-count').textContent = '미연결';
    $('#nav-note-count').textContent = '–';
  }
  function renderStats() {
    const values = [
      ['인계 상담 기록', state.rows.length, 'phone', 'violet', '통화별 발화 기록'],
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
      total.append(document.createTextNode(state.connected ? String(number) : '–'), node('small', '건'));
      const bottom = node('div', null, 'stat-bottom');
      bottom.append(node('span', state.connected ? explanation : '연결 확인 중'));
      article.append(head, total, bottom);
      stats.append(article);
    }
    $('#nav-call-count').textContent = state.connected ? String(state.rows.length) : '–';
    const urgentCount = state.rows.filter(row => row.answer_status === 'emergency').length;
    $('#notification-dot').hidden = !state.connected || urgentCount === 0;
  }
  function filtered() {
    const query = state.search.toLocaleLowerCase();
    return state.rows.filter(row => {
      if (state.filter !== 'all' && displayStatus(row) !== state.filter) return false;
      if (!query) return true;
      return [row.phone, row.user_text, row.assistant_text].some(value => String(value || '').toLocaleLowerCase().includes(query));
    });
  }
  function cell(text, className) { return node('td', text, className); }
  function renderTable() {
    const rows = filtered();
    state.page = Math.max(0, Math.min(state.page, Math.ceil(rows.length / pageSize) - 1));
    const page = rows.slice(state.page * pageSize, (state.page + 1) * pageSize);
    const title = $('#list-title');
    title.replaceChildren(document.createTextNode('인계된 상담 '), node('span', state.connected ? String(rows.length) : '연결 확인 중', 'count-badge'));
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
        cell(datetime(row.occurred_at), 'time-cell'), kind, cell('미연결'), arrow);
      const select = () => { state.selected = rowKey(row); renderTable(); renderDetail(); };
      tr.addEventListener('click', select);
      tr.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(); } });
      table.append(tr);
    }
    if (!page.length) {
      const tr = node('tr', null, 'empty-row');
      const td = node('td'); td.colSpan = 7;
      td.append(node('div', state.connected ? '조건에 맞는 인계 상담이 없습니다.' : '상담 수신함 연결 상태를 확인해 주세요.', 'empty-state'));
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
  function renderDetail() {
    const row = state.rows.find(item => rowKey(item) === state.selected);
    detail.replaceChildren();
    if (!row) {
      detail.append(node('div', state.connected ? '목록에서 상담을 선택해 주세요.' : '상담 수신함 연결을 확인하고 있습니다.', 'empty-state'));
      return;
    }
    const heading = node('div', null, 'detail-heading');
    heading.append(node('h2', '인계 상담 상세'), badge(displayStatus(row)));
    const person = node('div', null, 'detail-person');
    const copy = node('div');
    copy.append(node('h3', '상담자 정보 미확인'), node('p', textOr(row.phone, '연락처 미확인')));
    person.append(node('span', '상', 'detail-avatar'), copy);
    const body = node('div', null, 'detail-tab-content');
    field(body, '상담 시각', datetime(row.occurred_at));
    field(body, '병원 인계 시각', datetime(row.handed_at));
    field(body, '사용자 문의', row.user_text);
    if (row.answer_status !== 'interrupted' && row.answer_status !== 'emergency') field(body, '재생 상태', playbackStatus(row));
    field(body, row.answer_status === 'interrupted' ? '안내 상태' : 'AI 생성 답변', row.assistant_text);
    field(body, '환자 차트', '기존 의료기록과 연결되지 않았습니다.');
    field(body, '현재 위치', '위치 정보가 연결되지 않았습니다.');
    detail.append(heading, person, body, node('div', 'AI 답변은 의료진이 확인해 주세요.', 'detail-footer'));
  }
  function renderNotifications() {
    const urgent = state.rows.filter(row => row.answer_status === 'emergency');
    notification.replaceChildren(node('strong', state.connected ? `긴급 안내 ${urgent.length}건` : '연결 후 확인 가능합니다.'));
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
    if (state.connected && !urgent.length) notification.append(node('div', '긴급 안내가 없습니다.', 'empty-state'));
  }
  function render() {
    renderStats(); renderTable(); renderDetail(); renderNotifications(); showUnlinked();
    count.textContent = state.connected ? `${state.rows.length}건` : '연결 확인 중';
    results.replaceChildren(node('p', state.connected ? '아래 목록에서 인계된 상담을 선택하세요.' : '', 'consultation-note'));
  }
  function clearSession(message = '') {
    state.generation += 1; state.connected = false; state.rows = []; state.playback = new Set(); state.selected = null;
    state.search = ''; state.filter = 'all'; state.page = 0;
    $('#search-input').value = ''; notification.hidden = true;
    $('#notifications-button').setAttribute('aria-expanded', 'false');
    status.textContent = message; render();
  }
  async function refresh() {
    if (state.pending) return false;
    const generation = state.generation;
    state.pending = true;
    status.textContent = '인계된 상담을 불러오는 중입니다.';
    try {
      const response = await fetch('/api/team/municipality-handoffs', {cache: 'no-store'});
      if (response.status === 401 || response.status === 403) {
        if (generation === state.generation) {
          state.authBlocked = true; retry.hidden = false;
          clearSession('접속을 확인할 수 없습니다. 웹 로그인과 서버 상태를 확인해 주세요.');
        }
        return false;
      }
      if (!response.ok) throw new Error('상담 수신함을 불러오지 못했습니다.');
      const data = await response.json();
      if (!Array.isArray(data.handoffs)) throw new Error('상담 응답 형식을 확인할 수 없습니다.');
      if (generation !== state.generation) return false;
      const unique = new Map();
      const playback = new Set();
      for (const handoff of data.handoffs) {
        if (!handoff || !Array.isArray(handoff.events)) continue;
        if (typeof handoff.call_id === 'string' && Array.isArray(handoff.playback_completed_event_ids)) {
          for (const id of handoff.playback_completed_event_ids) if (typeof id === 'string' && id.startsWith('playback:')) playback.add(JSON.stringify([handoff.call_id, id]));
        }
        for (const row of handoff.events) {
          if (!row || typeof row !== 'object' || typeof row.event_id !== 'string' || typeof row.source !== 'string') continue;
          const previous = unique.get(rowKey(row));
          if (!previous || String(handoff.handed_at) > String(previous.handed_at)) unique.set(rowKey(row), {...row, handed_at: handoff.handed_at});
        }
      }
      state.rows = [...unique.values()].sort((a, b) => String(b.handed_at).localeCompare(String(a.handed_at)));
      state.playback = playback;
      state.connected = true;
      state.authBlocked = false; retry.hidden = true;
      if (!state.rows.some(row => rowKey(row) === state.selected)) state.selected = state.rows.length ? rowKey(state.rows[0]) : null;
      status.textContent = '';
      render();
      return true;
    } catch {
      if (generation === state.generation) {
        retry.hidden = false;
        clearSession('상담 수신함을 불러오지 못했습니다. 서버 상태를 확인해 주세요.');
      }
      return false;
    } finally { state.pending = false; }
  }
  retry.addEventListener('click', () => { state.authBlocked = false; void refresh(); });
  $('#status-tabs').addEventListener('click', event => {
    const button = event.target.closest('[data-filter]');
    if (button) { state.filter = button.dataset.filter; state.page = 0; renderTable(); }
  });
  $('#search-input').addEventListener('input', event => { state.search = event.target.value.trim(); state.page = 0; renderTable(); });
  $('#prev-page').addEventListener('click', () => { state.page -= 1; renderTable(); });
  $('#next-page').addEventListener('click', () => { state.page += 1; renderTable(); });
  $('#export-button').addEventListener('click', () => {
    if (!state.connected || !state.rows.length) {
      status.textContent = '내보낼 인계 상담이 없습니다.';
      return;
    }
    const quote = value => `"${String(value ?? '').replace(/^[\s\u0000-\u001f]*[=+@-]/, "'$&").replace(/"/g, '""')}"`;
    const entries = [['상담 시각', '연락처', '사용자 문의', 'AI 생성 답변', '안내 상태', '재생 상태'],
      ...filtered().map(row => [datetime(row.occurred_at), row.phone, row.user_text, row.assistant_text, labels[displayStatus(row)] || '상태 미확인', playbackStatus(row)])];
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
      dashboard: ['대시보드', '방문 준비 데스크', '지자체에서 인계한 상담을 확인하세요.'],
      history: ['통화 기록', '인계된 상담 기록', '병원에 인계된 상담을 살펴보세요.'],
      locations: ['위치 현황', '위치 현황', '현재 위치 정보는 연결되지 않았습니다.'],
      notes: ['직원 메모', '직원 메모', '직원 메모는 현재 연결되지 않았습니다.'],
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
  void refresh();
  setInterval(() => { if (!state.authBlocked && !document.hidden) void refresh(); }, 5000);
})();
