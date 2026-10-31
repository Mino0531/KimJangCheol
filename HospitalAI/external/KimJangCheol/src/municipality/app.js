(() => {
  const $ = (selector) => document.querySelector(selector);
  const all = (selector) => [...document.querySelectorAll(selector)];
  const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const formatTime = (value) => {
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? '시간 미확인' : new Intl.DateTimeFormat('ko-KR', {dateStyle:'short',timeStyle:'short',timeZone:'Asia/Seoul'}).format(date);
  };
  const statuses = {received:'확인 필요',acknowledged:'직원 확인',in_progress:'조치 중',handed_over:'병원 인계',completed:'조치 완료'};
  const types = {call_started:'전화 접수',call_ended:'통화 종료',caller_utterance:'주민 말씀',privacy_redacted:'원문 확인 제한',turn_completed:'상담 내용',turn_interrupted:'중단된 상담',urgent:'긴급 안내',recommendation:'병원 추천 정보'};
  let token = '', cases = [], currentId = null, filter = 'all', connected = false, busy = false, loaded = false, authenticating = false;
  let seen = new Set();
  let pollTimer = null, refreshSequence = 0;
  let actorName = '', workflowNote = '', detailVersion = null, detailCallId = null;
  const urgentReminders = new Map();

  function error(message) { const el = $('#error-banner'); el.textContent = message; el.hidden = !message; }
  function toast(message) { $('#toast').textContent = message; $('#toast').classList.add('show'); setTimeout(() => $('#toast').classList.remove('show'), 4000); }
  async function request(path, options = {}) {
    const response = await fetch(path, {cache:'no-store', ...options, headers:{Authorization:`Bearer ${token}`, ...(options.body ? {'Content-Type':'application/json'} : {})}});
    if (!response.ok) {
      if (response.status === 401) throw new Error('인증이 만료되었거나 인증값이 올바르지 않습니다.');
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
  function remindUrgent(next) {
    const now = Date.now(), pending = new Set();
    for (const item of next) {
      if (!item.urgent || !item.unread || item.status === 'completed') continue;
      pending.add(item.call_id);
      const urgentEvent = (Array.isArray(item.events) ? item.events : []).filter((entry) => entry.event_type === 'urgent').at(-1);
      const key = urgentEvent?.event_id || item.call_id;
      const prior = urgentReminders.get(item.call_id);
      const occurred = Date.parse(urgentEvent?.occurred_at || item.created_at);
      const reminder = prior && prior.key === key ? prior : {key, nextAt: (Number.isFinite(occurred) ? occurred : now) + 120000};
      if (now >= reminder.nextAt) {
        toast('확인하지 않은 긴급 상담이 있습니다.');
        if ('Notification' in window && Notification.permission === 'granted' && document.visibilityState !== 'visible') {
          try { new Notification('긴급 상담 확인 필요', {body:'지자체 대시보드에서 확인해 주세요.',tag:'municipal-urgent-reminder'}); }
          catch { $('#browser-alert-status').textContent = '브라우저 알림을 사용할 수 없습니다. 화면에서 확인해 주세요.'; }
        }
        reminder.nextAt = now + 300000;
      }
      urgentReminders.set(item.call_id, reminder);
    }
    for (const id of urgentReminders.keys()) if (!pending.has(id)) urgentReminders.delete(id);
  }
  async function refresh(showToast = false) {
    if (!token || busy) return;
    const sequence = ++refreshSequence;
    try {
      const result = await request('/api/municipality/inbox');
      if (sequence !== refreshSequence || !token) return;
      const next = Array.isArray(result.cases) ? result.cases : [];
      const fresh = next.filter((item) => item.unread && !seen.has(`${item.call_id}:${item.version}`));
      announce(fresh);
      remindUrgent(next);
      seen = new Set(next.map((item) => `${item.call_id}:${item.version}`));
      cases = next; connected = true; loaded = true; error('');
      $('#connection-status').textContent = '접수 기록 연결됨';
      $('#sync-label').textContent = `최근 확인 ${formatTime(new Date().toISOString())}`;
      render();
      if (showToast) toast('최신 상담을 확인했습니다.');
    } catch (err) {
      if (sequence !== refreshSequence || !token) return;
      connected = false; error(err.message || '접수 기록을 불러오지 못했습니다.');
      $('#connection-status').textContent = '연결 확인 필요'; $('#sync-label').textContent = '갱신 실패'; render();
    }
  }
  function stat(label, number, description, style = '') {
    return `<article class="stat"><div class="stat-head">${label}<span class="stat-icon ${style}" aria-hidden="true">●</span></div><div class="stat-number">${number}<small>건</small></div><div class="stat-foot">${description}</div></article>`;
  }
  function renderStats() {
    if (!token || !connected) { $('#nav-unread').textContent = '–'; $('#nav-urgent').textContent = '–'; $('#notification-count').textContent = '–'; $('#stats').replaceChildren(); $('#urgent-summary').textContent = '연결 상태를 확인할 수 없습니다.'; return; }
    const unread = cases.filter((item) => item.unread).length, urgent = cases.filter((item) => item.urgent && item.status !== 'completed').length, handed = cases.filter((item) => item.status === 'handed_over').length;
    $('#nav-unread').textContent = unread; $('#nav-urgent').textContent = urgent; $('#notification-count').textContent = unread;
    $('#stats').innerHTML = stat('전체 접수', cases.length, '전화 상담 기록') + stat('미확인 상담', unread, '새 내용 확인 필요') + stat('미완료 긴급', urgent, '직원 조치 필요', 'safety') + stat('병원 인계', handed, '직원이 인계한 상담', 'purple');
    $('#urgent-summary').textContent = urgent ? `미완료 긴급 상담 ${urgent}건이 있습니다. 긴급 목록에서 확인해 주세요.` : '현재 미완료 긴급 상담이 없습니다.';
  }
  function visibleCases() {
    const query = $('#search').value.trim().toLowerCase();
    return cases.filter((item) => (!query || String(item.phone || '').toLowerCase().includes(query)) && (filter === 'all' || (filter === 'unread' && item.unread) || (filter === 'urgent' && item.urgent) || (filter === 'handed_over' && item.status === 'handed_over')))
      .sort((a,b) => Number(Boolean(b.urgent && b.unread))-Number(Boolean(a.urgent && a.unread)) || String(b.updated_at).localeCompare(String(a.updated_at)));
  }
  function renderList() {
    const rows = visibleCases(); $('#case-count').textContent = rows.length;
    $('#case-table').innerHTML = rows.map((item) => `<tr tabindex="0" role="button" aria-label="${escapeHtml(item.phone || '번호 미확인')} 상담 상세 보기" data-call="${escapeHtml(item.call_id)}" class="${item.call_id === currentId ? 'selected' : ''}"><td><strong>${escapeHtml(item.phone || '번호 미확인')}</strong></td><td>${escapeHtml(formatTime(item.updated_at))}</td><td>${item.urgent ? '<span class="tag safety">긴급 확인</span>' : '일반'}</td><td><span class="case-status">${escapeHtml(item.unread ? '새 내용 확인 필요' : (statuses[item.status] || '상태 확인 필요'))}</span></td><td aria-hidden="true">›</td></tr>`).join('') || '<tr><td colspan="5" class="empty">해당하는 전화 상담이 없습니다.</td></tr>';
    for (const [index, item] of rows.entries()) if (item.urgent && item.status === 'completed') {
      $('#case-table').rows[index].querySelector('.tag.safety').textContent = '긴급';
    }
    all('[data-filter]').forEach((button) => {button.classList.toggle('selected', button.closest('#filters') && button.dataset.filter === filter); if (button.closest('#filters')) button.setAttribute('aria-pressed', String(button.dataset.filter === filter)); if (button.classList.contains('nav-item')) { button.classList.toggle('active', button.dataset.filter === filter); if (button.dataset.filter === filter) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current'); }});
  }
  function recommendationDetails(data) {
    if (!data || !Array.isArray(data.hospitals)) return '';
    const items = data.hospitals.slice(0,3).map((hospital) => `<li>${escapeHtml(hospital.name || '병원 이름 미확인')}${hospital.phone ? ` · ${escapeHtml(hospital.phone)}` : ''}${hospital.distance_km != null ? ` · 직선거리 ${escapeHtml(hospital.distance_km)}km` : ''}</li>`).join('');
    return `<div class="call-result"><strong>추천 정보</strong><ul>${items || '<li>추천 병원을 확인하지 못했습니다.</li>'}</ul>${data.warning ? `<p>${escapeHtml(data.warning)}</p>` : ''}<small>추천 정보는 상담 확인용이며 실제 의료기관 접수 여부는 별도 확인이 필요합니다.</small></div>`;
  }
  function renderDetail() {
    const item = cases.find((entry) => entry.call_id === currentId), panel = $('#detail-panel');
    if (!item) { panel.innerHTML = '<div class="empty">전화 상담을 선택해 주세요.</div>'; return; }
    if (!busy && detailCallId === item.call_id && detailVersion === item.version && panel.contains(document.activeElement)
        && document.activeElement.matches('input, textarea')) return;
    detailCallId = item.call_id;
    detailVersion = item.version;
    const entries = Array.isArray(item.events) ? item.events : [];
    const interrupted = new Set(entries.filter((entry) => entry.event_type === 'turn_interrupted' && typeof entry.event_id === 'string' && entry.event_id.startsWith('interrupted:')).map((entry) => JSON.stringify([entry.source, entry.event_id.slice('interrupted:'.length)])));
    const played = new Set(entries.filter((entry) => entry.event_type === 'playback_completed' && typeof entry.event_id === 'string' && entry.event_id.startsWith('playback:')).map((entry) => JSON.stringify([entry.source, entry.event_id.slice('playback:'.length)])));
    const answered = new Set(entries.filter((entry) => ['turn_completed', 'turn_interrupted'].includes(entry.event_type) && entry.utterance_id).map((entry) => JSON.stringify([entry.source, entry.utterance_id])));
    const events = entries.filter((entry) => entry.event_type !== 'playback_completed' && !(entry.event_type === 'caller_utterance' && entry.utterance_id && answered.has(JSON.stringify([entry.source, entry.utterance_id])))).map((entry) => {
      const interruptedBeforeCompletion = entry.event_type === 'turn_completed' && typeof entry.event_id === 'string' && entry.event_id.startsWith('turn:') && interrupted.has(JSON.stringify([entry.source, entry.event_id.slice('turn:'.length)]));
      const label = entry.event_type === 'turn_interrupted' ? '안내 상태' : '상담 안내';
      const playback = entry.event_type === 'turn_completed' && typeof entry.event_id === 'string' && entry.event_id.startsWith('turn:') && played.has(JSON.stringify([entry.source, entry.event_id.slice('turn:'.length)]));
      const playbackStatus = entry.event_type === 'caller_utterance' ? '<p><b>안내 상태</b><br>안내 여부 미확인</p>' : entry.event_type !== 'turn_completed' ? '' : interruptedBeforeCompletion ? '<div class="urgent-note">안내가 중단되어 끝까지 전달됐는지 확인되지 않았습니다.</div>' : playback ? '<p><b>재생 상태</b><br>재생 완료 · 청취 여부 미확인</p>' : '<p><b>재생 상태</b><br>재생 여부 미확인</p>';
      const endReasons = {caller:'상담 종료 요청',hangup:'통화 종료',connection_lost:'연결 끊김',safety_stop:'음성 연결 오류',consent_declined:'동의하지 않음'};
      const ending = entry.event_type === 'call_ended' ? `<p><b>종료 사유</b><br>${escapeHtml(endReasons[entry.end_reason] || '사유 확인 필요')}</p>` : '';
      const callerText = entry.user_text ? `<p>${entry.event_type === 'privacy_redacted' ? '' : '<b>말씀하신 내용</b><br>'}${escapeHtml(entry.user_text)}</p>` : '';
      return `<article class="event-card"><div class="event-heading"><strong>${escapeHtml(types[entry.event_type] || '접수 내용')}</strong><time>${escapeHtml(formatTime(entry.occurred_at))}</time></div>${entry.event_type === 'urgent' ? '<div class="urgent-note">긴급 상담입니다. 위급하면 119에 연락하세요.</div>' : ''}${playbackStatus}${ending}${callerText}${entry.assistant_text ? `<p><b>${label}</b><br>${escapeHtml(entry.assistant_text)}</p>` : ''}${recommendationDetails(entry.recommendation)}</article>`;
    }).join('');
    const actionDisabled = !connected || busy;
    const canAck = item.unread || item.status === 'received';
    const hasHandoffContent = entries.some((entry) => ['caller_utterance', 'privacy_redacted', 'turn_completed', 'turn_interrupted', 'urgent'].includes(entry.event_type));
    const canHandoff = !item.unread && ['acknowledged', 'in_progress'].includes(item.status) && hasHandoffContent;
    panel.innerHTML = `<div class="detail-heading"><div><span class="eyebrow">전화 상담</span><h2>${escapeHtml(item.phone || '번호 미확인')}</h2><p>최근 접수 ${escapeHtml(formatTime(item.updated_at))}</p></div>${item.urgent ? '<span class="tag safety">긴급 확인</span>' : '<span class="tag">일반 상담</span>'}</div>${item.urgent ? '<div class="detail-safety-alert">긴급 상담입니다. 먼저 확인해 주세요.</div>' : ''}<div class="detail-content"><div class="summary-box"><strong>현재 처리 상태</strong><p>${escapeHtml(item.unread ? '새 상담 내용을 확인해 주세요.' : (statuses[item.status] || '상태 확인 필요'))}</p></div><h4 class="section-title">접수 및 상담 내용</h4>${events || '<div class="empty">상담 내용이 아직 없습니다.</div>'}<div class="action-area"><button type="button" class="button secondary full" id="ack" ${actionDisabled || !canAck ? 'disabled' : ''}>상담 내용 확인</button><button type="button" class="button primary full" id="handoff" ${actionDisabled || !canHandoff ? 'disabled' : ''}>병원 대시보드에 인계</button>${!hasHandoffContent ? '<p>인계할 상담 또는 긴급 내용이 아직 도착하지 않았습니다.</p>' : ''}<p>확인 후 인계하면 같은 시스템의 병원 대시보드에 저장됩니다. 외부 병원에 자동 연락하거나 예약하지 않습니다.</p></div></div>`;
    const digest = document.createElement('section'); panel.querySelector('.detail-content').prepend(digest);
    window.ConsultationDigest?.mount(digest, {scope:'municipality', callId:item.call_id, version:item.version});
    if (item.urgent && item.status === 'completed') {
      panel.querySelector('.detail-heading .tag').textContent = '긴급 · 조치 완료';
      panel.querySelector('.detail-safety-alert').textContent = '긴급 상담 · 직원 조치가 완료되었습니다.';
    }
    if (item.hospital_handed_over || item.status === 'handed_over') {
      const hospital = item.hospital_workflow || {};
      const hospitalStates = {received:'인계 기록 저장 · 확인 전',in_progress:'병원 검토 중',completed:'병원 직원 처리 완료'};
      panel.querySelector('.summary-box').insertAdjacentHTML('afterend', `<div class="summary-box hospital-workflow"><strong>병원 대시보드 처리 상태</strong><p>${escapeHtml(hospitalStates[hospital.status] || '상태 확인 필요')}</p>${hospital.actor_name ? `<p>담당자 표시명: ${escapeHtml(hospital.actor_name)}</p>` : ''}${hospital.note ? `<p>${escapeHtml(hospital.note)}</p>` : ''}</div>`);
    }
    const history = Array.isArray(item.workflow_history) ? item.workflow_history : [];
    const historyActions = {start:'조치 시작',complete:'조치 완료'};
    const historyHtml = history.map((entry) => `<li><strong>${escapeHtml(statuses[entry.status] || historyActions[entry.action] || '처리 기록')}</strong> · ${escapeHtml(entry.actor_name || '담당자 미기록')} · ${escapeHtml(formatTime(entry.occurred_at || entry.created_at))}${entry.note ? `<p>${escapeHtml(entry.note)}</p>` : ''}</li>`).join('');
    const canStart = !item.unread && item.status === 'acknowledged';
    const canComplete = !item.unread && ['acknowledged', 'in_progress', 'handed_over'].includes(item.status);
    panel.querySelector('.action-area').insertAdjacentHTML('beforeend', `<form id="workflow-form" class="workflow-form"><h4>직원 조치</h4><label for="actor-name">담당자 표시명</label><input id="actor-name" name="actor_name" maxlength="100" required value="${escapeHtml(actorName)}" autocomplete="off"><label for="workflow-note">처리 메모 <span>(선택)</span></label><textarea id="workflow-note" name="note" maxlength="1000" rows="3">${escapeHtml(workflowNote)}</textarea><div class="workflow-actions"><button class="button secondary" type="submit" name="action" value="start" ${actionDisabled || !canStart ? 'disabled' : ''}>조치 시작</button><button class="button secondary" type="submit" name="action" value="complete" ${actionDisabled || !canComplete ? 'disabled' : ''}>조치 완료</button></div><p>표시명은 직접 입력한 이름입니다. 완료는 직원 조치 상태를 뜻합니다.</p></form>${historyHtml ? `<section class="workflow-history"><h4>처리 이력</h4><ol>${historyHtml}</ol></section>` : ''}`);
  }
  function render() { renderStats(); renderList(); renderDetail(); }
  function lock() {
    token = ''; cases = []; currentId = null; seen.clear(); urgentReminders.clear(); actorName = ''; workflowNote = ''; detailVersion = null; detailCallId = null; loaded = false; connected = false; refreshSequence++; clearInterval(pollTimer); pollTimer = null;
    $('#staff-token').value = ''; $('#search').value = ''; $('#workspace-content').hidden = true; $('#auth-panel').hidden = false; $('#connection-status').textContent = '접속 대기'; error(''); render();
  }
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
  async function workflow(action) {
    const item = cases.find((entry) => entry.call_id === currentId);
    if (!item || !connected || busy || !['start', 'complete'].includes(action)) return;
    const name = actorName.trim(), note = workflowNote.trim();
    if (!name) { error('담당자 표시명을 입력해 주세요.'); return; }
    busy = true; refreshSequence++; render();
    let actionError = '';
    try {
      const result = await request(`/api/municipality/inbox/${encodeURIComponent(item.call_id)}/workflow`, {method:'POST',body:JSON.stringify({expected_version:item.version,action,actor_name:name,note})});
      if (!result.case || result.case.status !== (action === 'start' ? 'in_progress' : 'completed')) throw new Error('처리 상태를 확인하지 못했습니다. 최신 기록을 확인해 주세요.');
      workflowNote = '';
      toast(action === 'start' ? '조치 시작을 기록했습니다.' : '조치 완료를 기록했습니다.');
    } catch (err) { actionError = err.message || '요청을 처리하지 못했습니다.'; }
    finally {busy = false; await refresh(); if (actionError) error(actionError);}
  }
  $('#auth-form').addEventListener('submit', async (event) => {
    event.preventDefault(); if (authenticating) return; token = $('#staff-token').value.trim(); if (!token) return;
    authenticating = true; $('#auth-form button').disabled = true;
    try {
      await refresh();
      if (connected) { $('#staff-token').value = ''; $('#auth-panel').hidden = true; $('#workspace-content').hidden = false; pollTimer = setInterval(() => { void refresh(); }, 8000); }
      else { token = ''; render(); }
    } finally { authenticating = false; $('#auth-form button').disabled = false; }
  });
  $('#lock').addEventListener('click', lock);
  $('#refresh').addEventListener('click', () => void refresh(true));
  $('#search').addEventListener('input', renderList);
  $('#detail-panel').addEventListener('input', (event) => {
    if (event.target.id === 'actor-name') actorName = event.target.value;
    if (event.target.id === 'workflow-note') workflowNote = event.target.value;
  });
  $('#detail-panel').addEventListener('submit', (event) => {
    if (event.target.id !== 'workflow-form') return;
    event.preventDefault();
    if (event.submitter) void workflow(event.submitter.value);
  });
  document.addEventListener('click', (event) => {
    const filterButton = event.target.closest('[data-filter]'); if (filterButton) {filter = filterButton.dataset.filter; $('#sidebar').classList.remove('open'); $('#menu-toggle').setAttribute('aria-expanded', 'false'); render();}
    const row = event.target.closest('[data-call]'); if (row) {if (currentId !== row.dataset.call) workflowNote = ''; currentId = row.dataset.call; render(); if (innerWidth < 1050) $('#detail-panel').scrollIntoView({block:'start'});}
    if (event.target.closest('#ack')) void act('ack');
    if (event.target.closest('#handoff')) void act('handoff');
  });
  $('#case-table').addEventListener('keydown', (event) => { const row = event.target.closest('[data-call]'); if (row && (event.key === 'Enter' || event.key === ' ')) {event.preventDefault(); if (currentId !== row.dataset.call) workflowNote = ''; currentId = row.dataset.call; render();} });
  $('#notification-filter').addEventListener('click', () => {filter = 'unread'; render();});
  $('#menu-toggle').addEventListener('click', () => { const open = $('#sidebar').classList.toggle('open'); $('#menu-toggle').setAttribute('aria-expanded', String(open)); });
  $('#browser-alerts').addEventListener('click', async () => {
    if (!('Notification' in window)) {$('#browser-alert-status').textContent = '이 브라우저는 알림을 지원하지 않습니다.'; return;}
    try {
      const permission = await Notification.requestPermission();
      $('#browser-alert-status').textContent = permission === 'granted' ? '브라우저 알림 켜짐 · 화면을 열어둔 동안 동작' : '브라우저 알림을 사용할 수 없습니다. 화면에서 확인해 주세요.';
    } catch { $('#browser-alert-status').textContent = '브라우저 알림을 사용할 수 없습니다. 화면에서 확인해 주세요.'; }
  });
})();
