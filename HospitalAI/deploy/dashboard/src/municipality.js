(() => {
  const $ = (selector) => document.querySelector(selector);
  const all = (selector) => [...document.querySelectorAll(selector)];
  const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const formatTime = (value) => {
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? '시간 미확인' : new Intl.DateTimeFormat('ko-KR', {dateStyle:'short',timeStyle:'short',timeZone:'Asia/Seoul'}).format(date);
  };
  const statuses = {received:'확인 필요',acknowledged:'직원 확인',handed_over:'병원 인계'};
  const types = {call_started:'전화 접수',turn_completed:'상담 내용',turn_interrupted:'중단된 상담',urgent:'긴급 안내',recommendation:'병원 추천 정보'};
  let cases = [], currentId = null, filter = 'all', connected = false, busy = false, loaded = false, authBlocked = false;
  let seen = new Set();
  let refreshSequence = 0;

  function error(message) { const el = $('#error-banner'); el.textContent = message; el.hidden = !message; }
  function toast(message) { $('#toast').textContent = message; $('#toast').classList.add('show'); setTimeout(() => $('#toast').classList.remove('show'), 4000); }
  async function request(path, options = {}) {
    const response = await fetch(path, {cache:'no-store', ...options, headers:{...(options.body ? {'Content-Type':'application/json'} : {})}});
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) {
        const failure = new Error('접속을 확인할 수 없습니다. 웹 로그인과 서버 상태를 확인해 주세요.');
        failure.authFailure = true;
        throw failure;
      }
      if (response.status === 409) throw new Error('다른 변경이 있거나 아직 확인하지 않은 상담입니다. 새 기록을 확인해 주세요.');
      throw new Error('요청을 처리하지 못했습니다. 연결을 확인하고 다시 시도해 주세요.');
    }
    return response.json();
  }
  function announce(newCases) {
    if (!loaded || !newCases.length) return;
    const urgent = newCases.some((item) => item.urgent);
    toast(urgent ? '긴급 상담이 접수됐습니다. 확인해 주세요.' : '새 전화 상담이 접수됐습니다.');
    if ('Notification' in window && Notification.permission === 'granted' && document.visibilityState !== 'visible') {
      try { new Notification(urgent ? '긴급 상담 확인 필요' : '새 전화 상담', {body:'지자체 대시보드에서 확인해 주세요.',tag:'municipal-intake'}); }
      catch { $('#browser-alert-status').textContent = '브라우저 알림을 사용할 수 없습니다. 화면에서 확인해 주세요.'; }
    }
  }
  async function refresh(showToast = false) {
    if (busy) return;
    const sequence = ++refreshSequence;
    try {
      const result = await request('/api/municipality/inbox');
      if (sequence !== refreshSequence) return;
      const next = Array.isArray(result.cases) ? result.cases : [];
      const fresh = next.filter((item) => item.unread && !seen.has(`${item.call_id}:${item.version}`));
      announce(fresh);
      seen = new Set(next.map((item) => `${item.call_id}:${item.version}`));
      cases = next; connected = true; loaded = true; authBlocked = false; $('#retry-connection').hidden = true; error('');
      $('#connection-status').textContent = '접수 기록 연결됨';
      $('#sync-label').textContent = `최근 확인 ${formatTime(new Date().toISOString())}`;
      render();
      if (showToast) toast('최신 상담을 확인했습니다.');
    } catch (err) {
      if (sequence !== refreshSequence) return;
      connected = false; cases = []; currentId = null; authBlocked = Boolean(err.authFailure); $('#retry-connection').hidden = false; error(err.message || '접수 기록을 불러오지 못했습니다.');
      $('#connection-status').textContent = '연결 확인 필요'; $('#sync-label').textContent = '갱신 실패'; render();
    }
  }
  function stat(label, number, description, style = '') {
    return `<article class="stat"><div class="stat-head">${label}<span class="stat-icon ${style}" aria-hidden="true">●</span></div><div class="stat-number">${number}<small>건</small></div><div class="stat-foot">${description}</div></article>`;
  }
  function renderStats() {
    if (!connected) { $('#nav-unread').textContent = '–'; $('#nav-urgent').textContent = '–'; $('#notification-count').textContent = '–'; $('#stats').replaceChildren(); $('#urgent-summary').textContent = '연결 상태를 확인할 수 없습니다.'; return; }
    const unread = cases.filter((item) => item.unread).length, urgent = cases.filter((item) => item.urgent && item.unread).length, handed = cases.filter((item) => item.status === 'handed_over').length;
    $('#nav-unread').textContent = unread; $('#nav-urgent').textContent = urgent; $('#notification-count').textContent = unread;
    $('#stats').innerHTML = stat('전체 접수', cases.length, '전화 상담 기록') + stat('미확인 상담', unread, '새 내용 확인 필요') + stat('미확인 긴급', urgent, '우선 확인 필요', 'safety') + stat('병원 인계', handed, '직원이 인계한 상담', 'purple');
    $('#urgent-summary').textContent = urgent ? `미확인 긴급 상담 ${urgent}건이 있습니다. 긴급 목록에서 확인해 주세요.` : '현재 미확인 긴급 상담이 없습니다.';
  }
  function visibleCases() {
    const query = $('#search').value.trim().toLowerCase();
    return cases.filter((item) => (!query || String(item.phone || '').toLowerCase().includes(query)) && (filter === 'all' || (filter === 'unread' && item.unread) || (filter === 'urgent' && item.urgent) || (filter === 'handed_over' && item.status === 'handed_over')))
      .sort((a,b) => Number(Boolean(b.urgent && b.unread))-Number(Boolean(a.urgent && a.unread)) || String(b.updated_at).localeCompare(String(a.updated_at)));
  }
  function renderList() {
    const rows = visibleCases(); $('#case-count').textContent = rows.length;
    $('#case-table').innerHTML = rows.map((item) => `<tr tabindex="0" role="button" aria-label="${escapeHtml(item.phone || '번호 미확인')} 상담 상세 보기" data-call="${escapeHtml(item.call_id)}" class="${item.call_id === currentId ? 'selected' : ''}"><td><strong>${escapeHtml(item.phone || '번호 미확인')}</strong></td><td>${escapeHtml(formatTime(item.updated_at))}</td><td>${item.urgent ? '<span class="tag safety">긴급 확인</span>' : '일반'}</td><td><span class="case-status">${escapeHtml(item.unread ? '새 내용 확인 필요' : (statuses[item.status] || '상태 확인 필요'))}</span></td><td aria-hidden="true">›</td></tr>`).join('') || '<tr><td colspan="5" class="empty">해당하는 전화 상담이 없습니다.</td></tr>';
    all('[data-filter]').forEach((button) => {button.classList.toggle('selected', button.closest('#filters') && button.dataset.filter === filter); if (button.closest('#filters')) button.setAttribute('aria-pressed', String(button.dataset.filter === filter)); if (button.classList.contains('nav-item')) { button.classList.toggle('active', button.dataset.filter === filter); if (button.dataset.filter === filter) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current'); }});
  }
  function recommendationDetails(data) {
    if (!data || !Array.isArray(data.hospitals)) return '';
    const items = data.hospitals.slice(0,3).map((hospital) => `<li>${escapeHtml(hospital.name || '병원 이름 미확인')}${hospital.phone ? ` · ${escapeHtml(hospital.phone)}` : ''}${hospital.distance_km != null ? ` · 직선거리 ${escapeHtml(hospital.distance_km)}km` : ''}</li>`).join('');
    return `<div class="call-result"><strong>추천 정보</strong><ul>${items || '<li>추천 병원을 확인하지 못했습니다.</li>'}</ul>${data.warning ? `<p>${escapeHtml(data.warning)}</p>` : ''}<small>추천 정보는 상담 확인용이며 병원 인계에 포함되지 않습니다.</small></div>`;
  }
  function renderDetail() {
    const item = cases.find((entry) => entry.call_id === currentId), panel = $('#detail-panel');
    if (!item) { panel.innerHTML = '<div class="empty">전화 상담을 선택해 주세요.</div>'; return; }
    const entries = Array.isArray(item.events) ? item.events : [];
    const interrupted = new Set(entries.filter((entry) => entry.event_type === 'turn_interrupted' && typeof entry.event_id === 'string' && entry.event_id.startsWith('interrupted:')).map((entry) => JSON.stringify([entry.source, entry.event_id.slice('interrupted:'.length)])));
    const played = new Set(entries.filter((entry) => entry.event_type === 'playback_completed' && typeof entry.event_id === 'string' && entry.event_id.startsWith('playback:')).map((entry) => JSON.stringify([entry.source, entry.event_id.slice('playback:'.length)])));
    const events = entries.filter((entry) => entry.event_type !== 'playback_completed').map((entry) => {
      const interruptedBeforeCompletion = entry.event_type === 'turn_completed' && typeof entry.event_id === 'string' && entry.event_id.startsWith('turn:') && interrupted.has(JSON.stringify([entry.source, entry.event_id.slice('turn:'.length)]));
      const label = entry.event_type === 'turn_interrupted' ? '안내 상태' : '상담 안내';
      const playback = entry.event_type === 'turn_completed' && typeof entry.event_id === 'string' && entry.event_id.startsWith('turn:') && played.has(JSON.stringify([entry.source, entry.event_id.slice('turn:'.length)]));
      const playbackStatus = entry.event_type !== 'turn_completed' ? '' : interruptedBeforeCompletion ? '<div class="urgent-note">안내가 중단되어 끝까지 전달됐는지 확인되지 않았습니다.</div>' : playback ? '<p><b>재생 상태</b><br>재생 완료 · 청취 여부 미확인</p>' : '<p><b>재생 상태</b><br>재생 여부 미확인</p>';
      return `<article class="event-card"><div class="event-heading"><strong>${escapeHtml(types[entry.event_type] || '접수 내용')}</strong><time>${escapeHtml(formatTime(entry.occurred_at))}</time></div>${entry.event_type === 'urgent' ? '<div class="urgent-note">긴급 상담입니다. 위급하면 119에 연락하세요.</div>' : ''}${playbackStatus}${entry.user_text ? `<p><b>말씀하신 내용</b><br>${escapeHtml(entry.user_text)}</p>` : ''}${entry.assistant_text ? `<p><b>${label}</b><br>${escapeHtml(entry.assistant_text)}</p>` : ''}${recommendationDetails(entry.recommendation)}</article>`;
    }).join('');
    const actionDisabled = !connected || busy;
    const canAck = item.unread || item.status === 'received';
    const hasHandoffContent = entries.some((entry) => entry.event_type === 'turn_completed' || entry.event_type === 'turn_interrupted' || entry.event_type === 'urgent');
    const canHandoff = !item.unread && item.status === 'acknowledged' && hasHandoffContent;
    panel.innerHTML = `<div class="detail-heading"><div><span class="eyebrow">전화 상담</span><h2>${escapeHtml(item.phone || '번호 미확인')}</h2><p>최근 접수 ${escapeHtml(formatTime(item.updated_at))}</p></div>${item.urgent ? '<span class="tag safety">긴급 확인</span>' : '<span class="tag">일반 상담</span>'}</div>${item.urgent ? '<div class="detail-safety-alert">긴급 상담입니다. 먼저 확인해 주세요.</div>' : ''}<div class="detail-content"><div class="summary-box"><strong>현재 처리 상태</strong><p>${escapeHtml(item.unread ? '새 상담 내용을 확인해 주세요.' : (statuses[item.status] || '상태 확인 필요'))}</p></div><h4 class="section-title">접수 및 상담 내용</h4>${events || '<div class="empty">상담 내용이 아직 없습니다.</div>'}<div class="action-area"><button type="button" class="button secondary full" id="ack" ${actionDisabled || !canAck ? 'disabled' : ''}>상담 내용 확인</button><button type="button" class="button primary full" id="handoff" ${actionDisabled || !canHandoff ? 'disabled' : ''}>병원 대시보드에 인계</button>${!hasHandoffContent ? '<p>인계할 상담 또는 긴급 내용이 아직 도착하지 않았습니다.</p>' : ''}<p>확인 후 인계하면 같은 시스템의 병원 대시보드에 저장됩니다. 외부 병원에 자동 연락하거나 예약하지 않습니다.</p></div></div>`;
  }
  function render() { renderStats(); renderList(); renderDetail(); }
  async function act(kind) {
    const item = cases.find((entry) => entry.call_id === currentId); if (!item || !connected || busy) return;
    busy = true; refreshSequence++; render();
    let actionError = '';
    try {
      const result = await request(`/api/municipality/inbox/${encodeURIComponent(item.call_id)}/${kind}`, {method:'POST',body:JSON.stringify({expected_version:item.version})});
      if (kind === 'handoff' && result.status !== 'handed_over') throw new Error('인계 상태를 확인하지 못했습니다. 최신 기록을 확인해 주세요.');
      toast(kind === 'ack' ? '상담 확인을 기록했습니다.' : '병원 대시보드에 인계했습니다.');
    } catch (err) { actionError = err.message || '요청을 처리하지 못했습니다.'; }
    finally {busy = false; await refresh(); if (actionError) error(actionError);}
  }
  $('#refresh').addEventListener('click', () => void refresh(true));
  $('#retry-connection').addEventListener('click', () => { authBlocked = false; void refresh(); });
  $('#search').addEventListener('input', renderList);
  document.addEventListener('click', (event) => {
    const filterButton = event.target.closest('[data-filter]'); if (filterButton) {filter = filterButton.dataset.filter; $('#sidebar').classList.remove('open'); $('#menu-toggle').setAttribute('aria-expanded', 'false'); render();}
    const row = event.target.closest('[data-call]'); if (row) {currentId = row.dataset.call; render(); if (innerWidth < 1050) $('#detail-panel').scrollIntoView({block:'start'});}
    if (event.target.closest('#ack')) void act('ack');
    if (event.target.closest('#handoff')) void act('handoff');
  });
  $('#case-table').addEventListener('keydown', (event) => { const row = event.target.closest('[data-call]'); if (row && (event.key === 'Enter' || event.key === ' ')) {event.preventDefault(); currentId = row.dataset.call; render();} });
  $('#notification-filter').addEventListener('click', () => {filter = 'unread'; render();});
  $('#menu-toggle').addEventListener('click', () => { const open = $('#sidebar').classList.toggle('open'); $('#menu-toggle').setAttribute('aria-expanded', String(open)); });
  $('#browser-alerts').addEventListener('click', async () => {
    if (!('Notification' in window)) {$('#browser-alert-status').textContent = '이 브라우저는 알림을 지원하지 않습니다.'; return;}
    try {
      const permission = await Notification.requestPermission();
      $('#browser-alert-status').textContent = permission === 'granted' ? '브라우저 알림 켜짐 · 화면을 열어둔 동안 동작' : '브라우저 알림을 사용할 수 없습니다. 화면에서 확인해 주세요.';
    } catch { $('#browser-alert-status').textContent = '브라우저 알림을 사용할 수 없습니다. 화면에서 확인해 주세요.'; }
  });
  render();
  void refresh();
  setInterval(() => { if (!authBlocked && !document.hidden) void refresh(); }, 8000);
})();
