(async () => {
'use strict';
const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
const paths = {sprout:'M12 21v-9 M12 15C4 15 3 10 3 5c6 0 9 4 9 10 M12 11c0-6 4-9 9-9 0 6-3 10-9 10',building:'M5 21V3h14v18 M2 21h20 M9 7h1 M14 7h1 M9 11h1 M14 11h1 M10 21v-5h4v5',grid:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',phone:'M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 2 .7 2.9a2 2 0 0 1-.5 2.1L8 10a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.5c.9.3 1.9.6 2.9.7a2 2 0 0 1 1.7 2z',link:'M10 13a5 5 0 0 0 7 .3l4-4a5 5 0 0 0-7-7l-2 2 M14 11a5 5 0 0 0-7-.3l-4 4a5 5 0 0 0 7 7l2-2',repeat:'m17 2 4 4-4 4 M3 11V8a2 2 0 0 1 2-2h16 M7 22l-4-4 4-4 M21 13v3a2 2 0 0 1-2 2H3',note:'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z M14 2v6h6 M8 13h8 M8 17h6',heart:'M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8z',chevron:'m9 5 7 7-7 7',left:'m15 5-7 7 7 7',home:'m3 10 9-7 9 7v10H15v-7H9v7H3z',refresh:'M21 3v6h-6 M3 21v-6h6 M4 9a8 8 0 0 1 13-5l4 5 M20 15a8 8 0 0 1-13 5l-4-5',plus:'M12 5v14 M5 12h14',search:'M21 21l-5-5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',pin:'M20 10c0 6-8 12-8 12S4 16 4 10a8 8 0 1 1 16 0z M15 10a3 3 0 1 1-6 0 3 3 0 0 1 6 0',download:'M12 3v12 m-5-5 5 5 5-5 M4 16v5h16v-5',activity:'M2 12h4l3-8 6 16 3-8h4',x:'m6 6 12 12 M6 18 18 6',arrow:'M4 12h16 m-6-6 6 6-6 6',check:'m5 12 4 4L19 6',copy:'M9 9h12v12H9z M5 15H3V3h12v2',shield:'M12 2 3 6v6c0 5 9 10 9 10s9-5 9-10V6z m-4 10 3 3 5-5',menu:'M3 6h18 M3 12h18 M3 18h18',users:'M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2 M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0 M23 21v-2a4 4 0 0 0-3-3.9'};
const icon = n => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[n] || paths.note}"/></svg>`;
$$('[data-icon]').forEach(e => e.innerHTML = icon(e.dataset.icon));
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = (v, date = false) => !v ? '미지정' : new Intl.DateTimeFormat('ko-KR', {timeZone:'Asia/Seoul', ...(date ? {month:'numeric',day:'numeric'} : {}), hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(v));
const uid = () => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
const apiMode = $('meta[name="municipality-data-source"]')?.content === 'api';
const Safety = window.MunicipalitySafety;
const KEY = 'kjc-municipality-v1', stages = {received:'접수',checking:'확인 중',hospital_requested:'병원 답변 대기',response_received:'재안내 필요',resident_informed:'안내 완료',completed:'처리 완료',unlinked:'미등록'};
const stageOrder = ['received','checking','hospital_requested','response_received','resident_informed','completed'];
const actionLabels = {case_created:'연계 업무 접수',metadata_updated:'담당·확인 기한 설정',hospital_requested:'병원에 확인 요청',hospital_responded:'병원 답변 도착',contact_logged:'주민 연락 기록',completed:'업무 처리 완료',note_added:'담당자 메모',safety_opened:'위기 대응·안부 확인 접수',safety_updated:'현재 위치·상황 확인',safety_contact_logged:'위기 대응 연락 기록',safety_handoff_recorded:'관계기관·담당자 인계 확인',safety_closed:'후속 안전 확인 완료'};
const outcomes = {connected:'통화 연결·안내',no_answer:'부재중',callback_requested:'다시 연락 요청'};
const state = {view:'overview',filter:'all',search:'',region:'',page:0,selected:'coordination-003',tab:'workflow'};
let db, busy = false, refreshing = false, toastTimer;
const detailCache = new Map(), drafts = new Map();
const draftFor = id => { if (!drafts.has(id)) drafts.set(id, {}); return drafts.get(id); };
async function api(path, options = {}) {
 const r = await fetch(path, {...options, headers:{'Content-Type':'application/json',...options.headers}});
 const data = await r.json().catch(() => ({}));
 if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `입력값을 확인해 주세요. (HTTP ${r.status})`);
 return data;
}
function toast(text) { $('#toast').textContent = text; $('#toast').classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').classList.remove('show'), 3800); }
function error(text) { $('#error-banner').textContent = text; $('#error-banner').hidden = !text; }
function persist() {
 if (apiMode) return;
 try { localStorage.setItem(KEY, JSON.stringify(db)); } catch { toast('브라우저 저장 공간을 사용할 수 없어 현재 화면에만 반영됩니다.'); }
}
try {
 if (apiMode) db = await api('/api/municipality/bootstrap');
 else { try { const saved = JSON.parse(localStorage.getItem(KEY)); if (saved?.fixture_version === 1 && Array.isArray(saved.cases)) db = saved; } catch {} if (!db) db = structuredClone(window.MunicipalityFixtures); }
} catch (e) { error('DB 기록을 불러오지 못했습니다. 서버를 확인한 뒤 새로고침해 주세요.'); $('#new-case').disabled = true; return; }
db.safety_guidance ||= window.MunicipalityFixtures.safety_guidance;
if (!apiMode) {
 for (const c of db.cases) if (!Object.hasOwn(c,'safety')) c.safety = structuredClone(window.MunicipalityFixtures.cases.find(f=>f.id===c.id)?.safety || null);
}
const caseFor = id => db.cases.find(c => c.id === id) || (id?.startsWith('unlinked:') ? unlinked(db.calls.find(c => c.id === id.slice(9))) : null);
function unlinked(call) { return call ? {id:`unlinked:${call.id}`,call_id:call.id,call,status:'unlinked',category:'업무 미등록',request_text:call.purpose,assignee:'—',priority:'normal',updated_at:call.started_at,version:0} : null; }
const current = () => caseFor(state.selected);
function filtered() {
 let rows = state.view === 'calls' ? db.calls.map(call => db.cases.find(c => c.call_id === call.id) || unlinked(call)) : db.cases;
 return rows.filter(c => {
  if (state.view === 'safety' && !c.safety) return false;
  if (state.view === 'hospital' && !c.hospital_request) return false;
  if (state.view === 'followup' && !(['response_received','resident_informed'].includes(c.status) || ['no_answer','callback_requested'].includes(c.last_contact_outcome))) return false;
  if (state.filter === 'attention' && (c.priority !== 'attention' || c.status === 'completed')) return false;
  if (state.filter === 'unassigned' && c.assignee !== '미배정') return false;
  if (state.filter === 'completed' && (state.view==='safety'?c.safety?.status!=='closed':c.status !== 'completed'||Safety.active(c))) return false;
  if (state.region && c.call.region !== state.region) return false;
  const q = state.search.toLowerCase(), digits = q.replace(/\D/g,'');
  return !q || `${c.call.name} ${c.call.phone} ${c.call.region} ${c.request_text} ${c.assignee} ${c.category}`.toLowerCase().includes(q) || (digits.length >= 3 && String(c.call.phone || '').replace(/\D/g,'').includes(digits));
 }).sort((a,b) => String(state.view === 'calls' ? b.call.started_at : b.updated_at).localeCompare(String(state.view === 'calls' ? a.call.started_at : a.updated_at)) || a.id.localeCompare(b.id));
}
const tag = c => Safety.active(c) ? `<span class="tag safety">${esc(Safety.labels[c.safety.status])}</span>` : `<span class="tag ${c.status}">${esc(stages[c.status])}</span>`;
function renderStats() {
 const a = db.cases, counts = [a.filter(Safety.active).length, a.filter(c=>c.status==='hospital_requested').length, a.filter(c=>!Safety.active(c)&&(['response_received','resident_informed'].includes(c.status)||['no_answer','callback_requested'].includes(c.last_contact_outcome))&&c.status!=='completed').length, a.filter(c=>c.status==='completed'&&!Safety.active(c)).length];
 const cards = [['위기 대응 진행','shield','safety','담당자 등록 기준','연락·인계 확인 중'],['병원 답변 대기','building','purple','병원 연결','방문 지원 요청'],['주민 재연락 필요','repeat','amber','후속 확인','답변·안부 다시 전달'],['방문 지원 완료','check','','주민 안내까지','연결 업무 마무리']];
 $('#stats').innerHTML = cards.map(([label,ic,color,foot,desc],i) => `<article class="stat"><div class="stat-head">${label}<span class="stat-icon ${color}">${icon(ic)}</span></div><div class="stat-number">${counts[i]}<small>건</small></div><div class="stat-foot ${color}"><strong>${foot}</strong> · ${desc}</div><div class="stat-spark" aria-hidden="true">${[9,14,12,18,15,22,25].map(h=>`<i style="height:${h}px"></i>`).join('')}</div></article>`).join('');
 $('#nav-calls').textContent = db.calls.length; $('#nav-hospital').textContent = counts[1]; $('#nav-safety').textContent = counts[0];
 $('#safety-board').innerHTML = Safety.board(db,esc,icon);
}
function renderTable() {
 const rows = filtered(), size = 7;
 state.page = Math.max(0, Math.min(state.page, Math.ceil(rows.length / size) - 1));
 const titles = {overview:'주민 연결·지원 목록',calls:'통화 내용 기록',hospital:'병원 연계 현황',followup:'주민 재안내 목록',history:'업무별 처리 기록',safety:'안전·위기 대응 목록'};
 $('#list-title').innerHTML = `${titles[state.view]} <span class="count-badge">${rows.length}</span>`;
 $('#list-subtitle').textContent = state.view === 'calls' ? '과거 통화까지 조회 · 미등록 통화도 확인할 수 있어요.' : '요청을 확인하고, 다음에 할 일을 이어가세요.';
 const page = rows.slice(state.page * size, (state.page + 1) * size);
 $('#case-table').innerHTML = page.map((c,i) => `<tr data-case="${esc(c.id)}" tabindex="0" aria-selected="${state.selected===c.id}" aria-label="${esc(c.call.name)} 주민 기록 열기" class="${state.selected===c.id?'selected':''}"><td><div class="person"><span class="avatar ${['','mint','blue','violet'][i%4]}">${esc(c.call.name?.[0])}</span><div><strong>${esc(c.call.name)}<small>${esc(c.call.age)}세</small></strong><small>${esc(c.call.region)}</small></div></div></td><td class="request-cell"><strong>${c.priority==='attention'&&c.status!=='completed'?'<i class="priority-mark"></i>':''}${esc(c.category)}</strong><small title="${esc(c.request_text)}">${esc(c.request_text)}</small></td><td><span class="assignee ${c.assignee==='미배정'?'unassigned':''}">${esc(c.assignee)}</span></td><td>${tag(c)}</td><td class="due">${c.due_at?esc(fmt(c.due_at)):c.status==='completed'?'처리 완료':'미지정'}<small>${state.view==='calls'?esc(fmt(c.call.started_at,true)):c.due_at?'담당자 확인 기한':'—'}</small></td><td class="row-arrow">${icon('chevron')}</td></tr>`).join('') || '<tr><td colspan="6" class="empty">조건에 맞는 기록이 없어요.<br>검색이나 필터를 바꿔보세요.</td></tr>';
 $('#page-range').textContent = `${rows.length ? state.page*size+1 : 0}–${Math.min((state.page+1)*size,rows.length)} / ${rows.length}`;
 $('#prev').disabled = state.page === 0; $('#next').disabled = (state.page+1)*size >= rows.length;
 $$('#filters button').forEach(b => { b.classList.toggle('selected',b.dataset.filter===state.filter); b.setAttribute('aria-pressed',String(b.dataset.filter===state.filter)); });
 $('#clear-filters').hidden = !(state.search || state.region || state.filter !== 'all');
}
function renderRegions() {
 const groups = new Map(); db.cases.forEach(c => groups.set(c.call.region || '지역 미확인',(groups.get(c.call.region || '지역 미확인')||0)+1));
 const rows = [...groups].sort((a,b)=>b[1]-a[1] || a[0].localeCompare(b[0]));
 $('#region-summary').innerHTML = `<strong>${rows.length}</strong>개 지역<span>지역 미확인 포함</span>`;
 $('#region-bars').innerHTML = rows.slice(0,5).map(([r,n])=>`<button class="region-row" data-region="${esc(r)}" aria-label="${esc(r)} ${n}건 보기"><span>${esc(r.replace('화순군 ',''))}</span><span class="region-track"><i style="width:${n/(rows[0]?.[1]||1)*100}%"></i></span><b>${n}건</b></button>`).join('');
 const regions = [...new Set(db.calls.map(c=>c.region).filter(Boolean))].sort();
 $('#region').innerHTML = '<option value="">전체 지역</option>'+regions.map(r=>`<option value="${esc(r)}" ${state.region===r?'selected':''}>${esc(r)}</option>`).join('');
}
function renderActivity() {
 const actions = [...db.actions].sort((a,b)=>b.created_at.localeCompare(a.created_at));
 $('#activity-list').innerHTML = actions.slice(0,4).map(a=>{const c=caseFor(a.case_id);return `<button class="activity-item" data-activity-case="${esc(a.case_id)}"><span class="activity-avatar">${esc(a.actor_name.slice(1,2))}</span><span><p><strong>${esc(c?.call.name || '주민')}</strong> 님의 ${esc(actionLabels[a.action_type]||'처리 기록')}<br>${esc(a.actor_name)} · ${esc(a.actor_role)}</p><small>${esc(fmt(a.created_at,true))}${a.is_fixture?' · 가상 기록':''}</small></span></button>`;}).join('') || '<div class="empty">아직 처리 기록이 없어요.</div>';
}
function progress(c) {
 if(Safety.active(c)){const keys=['checking','contacted','handed_over','closed'],idx=keys.indexOf(c.safety.status);return `<div class="progress-steps safety-progress" aria-label="위기 대응 진행 단계">${keys.map((k,i)=>`<span class="progress-step ${i<idx?'done':i===idx?'current':''}"><i>${i<idx?'✓':i+1}</i>${esc(Safety.labels[k])}</span>`).join('')}</div>`;}
 const short = ['접수','확인','병원 요청','답변 도착','주민 안내','완료'], idx = stageOrder.indexOf(c.status);
 return `<div class="progress-steps" aria-label="진행 단계: ${esc(stages[c.status])}">${short.map((l,i)=>`<span class="progress-step ${i<idx?'done':i===idx?'current':''}"><i>${i<idx?'✓':i+1}</i>${l}</span>`).join('')}</div>`;
}
function timeline(detail) {
 const rows = [...detail.actions].sort((a,b)=>b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id));
 return rows.map(a=>`<article class="timeline-item ${a.actor_role==='병원'?'hospital':''}"><strong>${esc(actionLabels[a.action_type]||'처리 기록')}${a.outcome?' · '+esc(outcomes[a.outcome]):''}${a.contact_target?' · '+esc(a.contact_target)+' / '+(a.contact_result==='connected'?'연결됨':'연결되지 않음'):''}</strong><small>${esc(a.actor_name)} · ${esc(a.actor_role)} · ${esc(fmt(a.created_at,true))}${a.is_fixture?' · 가상 이력':''}</small><p>${esc(a.note)}</p></article>`).join('') || '<div class="empty">처리 내역이 없어요.</div>';
}
function workflow(c) {
 const d = draftFor(c.id), closed = c.status === 'completed';
 const due = c.due_at ? new Date(Date.parse(c.due_at)+9*3600000).toISOString().slice(0,16) : '';
 let content = `<h4 class="section-title">${icon('note')}주민이 요청한 내용</h4><div class="summary-box">${esc(c.request_text)}</div>`;
 if (closed) content += '<div class="info-note">주민 재안내와 업무 처리가 완료되었어요. 추가로 확인한 내용은 아래 메모에 남겨 주세요.</div>';
 else content += `<form id="assignment-form"><h4 class="section-title">${icon('users')}담당자와 확인 기한</h4><div class="assignment-grid"><label class="field">담당자<select data-draft="assignee" name="assignee">${db.assignees.map(a=>`<option ${a===(d.assignee??c.assignee)?'selected':''}>${esc(a)}</option>`).join('')}</select></label><label class="field">확인 순서<select data-draft="priority" name="priority"><option value="normal" ${(d.priority??c.priority)==='normal'?'selected':''}>일반</option><option value="attention" ${(d.priority??c.priority)==='attention'?'selected':''}>우선 확인</option></select></label></div><label class="field">다음 확인 기한 (한국시간)<input data-draft="due_at" name="due_at" type="datetime-local" value="${esc(d.due_at??due)}"></label><div class="form-footer"><button class="button secondary" type="submit">${icon('check')}담당·기한 저장</button></div></form>`;
 if (Safety.active(c)) return content + '<div class="info-note">위기 대응이 진행 중이에요. 현재 위치·실제 연락·인계와 후속 확인을 먼저 이어가세요.</div><button class="button safety-button full" data-tab="safety">위기 대응 기록으로 이동</button>';
 if (['received','checking'].includes(c.status)) content += `<form class="form-section" id="hospital-request-form"><h4 class="section-title">${icon('building')}병원에 확인할 질문</h4><p class="section-caption">연계 병원 · 김장철병원 / 원무팀</p><label class="field">전달 내용<textarea name="note" data-draft="hospital_note" maxlength="1500" placeholder="접수 장소, 준비 안내 등 병원에 확인할 내용을 적어 주세요." required>${esc(d.hospital_note??c.request_text)}</textarea></label><button class="button primary full" type="submit">${icon('link')}병원 데스크에 확인 요청</button></form>`;
 if (c.hospital_request) content += `<div class="request-receipt"><strong>${c.status==='hospital_requested'?'병원 답변을 기다리고 있어요':'병원에 전달한 내용'}</strong><p>${esc(c.hospital_request)}</p><a href="${apiMode?'/':'index.html'}" target="_blank" rel="noopener">병원 대시보드에서 요청 보기 ↗</a></div>`;
 if (c.hospital_response) content += `<div class="response-card"><h4>김장철병원 회신<span class="tag completed">답변 도착</span></h4><p>${esc(c.hospital_response)}</p><small>원무팀 · ${esc(fmt(c.hospital_responded_at,true))} · 앱 내 가상 연계</small></div>`;
 if (!closed) content += `<form class="form-section" id="contact-form"><h4 class="section-title">${icon('phone')}주민에게 다시 안내하기</h4><p class="section-caption">실제 발신 후 담당자가 결과를 입력하는 항목입니다.</p><label class="field">연락 결과<select name="outcome" data-draft="outcome">${Object.entries(outcomes).map(([k,v])=>`<option value="${k}" ${(d.outcome??(c.status==='response_received'?'connected':'no_answer'))===k?'selected':''}>${v}</option>`).join('')}</select></label><label class="field">안내·연락 내용<textarea name="note" data-draft="contact_note" maxlength="1500" placeholder="전달한 안내와 주민의 반응, 부재중이면 다음 확인 사항을 적어 주세요." required>${esc(d.contact_note||'')}</textarea></label><button class="button secondary full" type="submit">${icon('check')}연락 결과 기록</button></form>`;
 if (c.last_contact_outcome) content += `<div class="info-note">최근 연락 · ${esc(outcomes[c.last_contact_outcome])}${c.last_contact_note?'<br>'+esc(c.last_contact_note):''}</div>`;
 if (c.status==='resident_informed') content += `<form class="form-section" id="complete-form"><h4 class="section-title">${icon('check')}후속 조치 마무리</h4><label class="field">마무리 내용<textarea name="note" data-draft="complete_note" maxlength="1500" required placeholder="요청과 안내가 확인되었는지 기록해 주세요.">${esc(d.complete_note||'')}</textarea></label><button class="button primary full" type="submit">${icon('check')}처리 완료로 기록</button></form>`;
 return content+`<form class="form-section" id="note-form"><h4 class="section-title">${icon('note')}다음 담당자에게 메모</h4><label class="field"><textarea name="note" data-draft="memo" maxlength="1500" required placeholder="진행 상황이나 추가 확인할 내용을 남겨 주세요.">${esc(d.memo||'')}</textarea></label><div class="form-footer"><button class="button secondary" type="submit">메모 저장</button></div></form>`;
}
function transcript(c,detail) {
 return `<h4 class="section-title">${icon('note')}통화 요약</h4><div class="summary-box">${esc(c.call.summary_text || (c.call.call_status==='in_progress'?'통화 중입니다. 요약이 아직 준비되지 않았어요.':'요약이 없는 통화입니다. 대화 기록을 확인해 주세요.'))}</div><div class="transcript-heading"><h4 class="section-title">${icon('phone')}확정된 대화 기록</h4><small>${detail.messages.length}개 발화 · 가상 통화</small></div><div class="transcript">${detail.messages.map(m=>`<div class="message ${m.speaker==='assistant'?'assistant':'caller'}"><small>${m.speaker==='assistant'?'AI 안내':esc(c.call.name)} · ${esc(fmt(m.occurred_at))}</small><p>${esc(m.text)}</p></div>`).join('') || '<div class="empty">아직 확정된 발화가 없어요.</div>'}</div>`;
}
function support(c,detail) {
 return `<h4 class="section-title">${icon('heart')}이동과 연락을 위한 지원 정보</h4><p class="section-caption">시연 시나리오에 설정된 정보 · 차트에서 추론하지 않음</p><div class="support-grid">${Object.entries({living:'거주·연락 상황',device:'연락 방식',mobility:'이동 도움',companion:'동행 여부'}).map(([k,l])=>`<div class="support-item"><small>${l}</small><strong>${esc(c.support?.[k]||'미확인')}</strong></div>`).join('')}</div><h4 class="section-title">${icon('building')}방문할 병원</h4><div class="summary-box">김장철병원 · 가상 병원<br>방문 목적: ${esc(c.call.purpose)}<br>방문 계획: ${esc(c.call.planned_visit_text || '미확인')}</div><h4 class="section-title">${icon('pin')}통화 당시 위치</h4><div class="summary-box">${esc(c.call.region)}${detail.locations.length?'<br>'+detail.locations.map(l=>`${esc(l.latitude??'미확인')}, ${esc(l.longitude??'미확인')} · 가상 좌표`).join('<br>'):'<br>좌표 미확인'}</div><div class="info-note">지자체 화면은 방문 지원에 필요한 요청·통화·연락 정보 중심입니다. 처방·복용약·진료 차트는 병원 화면에서 확인합니다. 차량 배차나 이동 수단 예약은 이 화면에서 실행되지 않습니다.</div>`;
}
async function loadDetail(c) {
 const key = `${c.id}:${c.version}`;
 if (detailCache.has(key)) return;
 detailCache.set(key,{loading:true});
 try {
  const detail = apiMode ? await api(c.status==='unlinked'?`/api/municipality/calls/${encodeURIComponent(c.call_id)}`:`/api/municipality/cases/${encodeURIComponent(c.id)}`) : {messages:db.messages.filter(m=>m.call_id===c.call_id).sort((a,b)=>a.sequence-b.sequence),locations:db.locations.filter(l=>l.call_id===c.call_id),actions:db.actions.filter(a=>a.case_id===c.id)};
  detailCache.set(key,detail);
 } catch (e) { detailCache.set(key,{error:e.message}); }
 if (current()?.id===c.id && current()?.version===c.version) renderDetail();
}
function renderDetail() {
 const c = current(), panel = $('#detail-panel');
 if (!c) { panel.innerHTML='<div class="empty">표시할 연계 업무가 없어요.<br>검색이나 필터를 바꿔보세요.</div>'; return; }
 const key = `${c.id}:${c.version}`, detail = detailCache.get(key);
 if (!detail) loadDetail(c);
 const tab = c.status==='unlinked'?'transcript':state.tab;
 let content = !detail || detail.loading ? '<div class="empty">기록을 불러오고 있어요.</div>' : detail.error ? `<div class="empty">${esc(detail.error)}<br><button class="text-button" id="retry-detail">다시 불러오기</button></div>` : tab==='safety'?Safety.panel(c,db.safety_guidance,draftFor(c.id),esc,icon,fmt):tab==='workflow'?workflow(c):tab==='transcript'?transcript(c,detail):tab==='support'?support(c,detail):timeline(detail);
 if(c.status==='unlinked') content = `<div class="info-note">아직 연계 업무가 없는 과거 통화예요.<br><button class="text-button" id="register-selected">이 통화에 업무 등록하기</button></div>`+content;
 panel.innerHTML=`<div class="detail-heading"><h2>${c.status==='unlinked'?'주민 통화 기록':'주민 정보 · 연계 업무'}</h2><span class="reference">${esc(c.call_id.replace('demo-call-','CALL #').replace('past-call-','PAST #'))}</span></div><div class="detail-person"><span class="detail-avatar">${esc(c.call.name?.[0])}</span><div><h3>${esc(c.call.name)}<small>${esc(c.call.age)}세</small></h3><div class="contact-line"><span>${esc(c.call.phone||'연락처 미확인')}</span><button class="icon-button" id="copy-phone" aria-label="연락처 복사" ${!c.call.phone?'disabled':''}>${icon('copy')}</button></div></div>${tag(c)}</div><div class="detail-facts"><div><label>거주·통화 지역</label><span>${esc(c.call.region)}</span></div><div><label>지원 유형</label><span>${esc(c.category)}</span></div><div><label>연계 병원</label><span>김장철병원</span></div><div><label>담당자</label><span>${esc(c.assignee)}</span></div></div>${c.status==='unlinked'?'':progress(c)}${Safety.active(c)?'<div class="detail-safety-alert">'+icon('shield')+'위기 대응 중 · 일반 방문 업무와 별도로 진행해요.</div>':''}<div class="detail-tabs" role="tablist" aria-label="주민 기록 항목">${(c.status==='unlinked'?[['transcript','통화 기록']]:[['workflow','병원 연결'],['safety','위기 대응'],['transcript','통화 기록'],['support','지원 정보'],['timeline','처리 이력']]).map(([k,l])=>`<button role="tab" id="detail-tab-${k}" aria-controls="detail-content" aria-selected="${tab===k}" tabindex="${tab===k?0:-1}" class="${tab===k?'active':''}" data-tab="${k}">${l}</button>`).join('')}</div><div class="detail-content" id="detail-content" role="tabpanel" aria-labelledby="detail-tab-${tab}">${content}</div><div class="detail-footer">${icon('shield')}가상 연계 시연 · 병원 회신과 주민 안내를 구분해 기록해요.</div>`;
 if (busy) panel.querySelectorAll('button[type=submit]').forEach(b=>b.disabled=true);
}
function render() {
 const titles={safety:['안전·위기 대응','도움이 필요한 순간을 놓치지 않도록','현재 상황과 위치를 확인하고, 실제 연락·인계·후속 확인을 기록합니다.'],overview:['업무 대시보드','주민 연결·안전 지원 데스크','어르신과 병원을 연결하고, 위기 상황의 연락과 후속 확인을 함께 이어갑니다.'],calls:['통화 내용 기록','한 통의 이야기, 이어지는 지원','오늘과 지난 통화의 확정 발화·요약을 확인하고 필요한 업무로 연결합니다.'],hospital:['병원 연계 현황','병원과 나누는 확인의 기록','전달한 요청과 병원 회신을 함께 확인하고 주민에게 안내할 내용을 정리합니다.'],followup:['주민 재안내','한 번 더 연락해야 할 분들','병원 답변이 도착했거나 다시 연락할 주민을 확인합니다.'],history:['처리 기록','다음 담당자에게 이어지는 맥락','업무별로 누가 무엇을 확인하고 전달했는지 살펴봅니다.']};
 const [crumb,title,desc]=titles[state.view];$('#breadcrumb').textContent=crumb;$('#page-title').innerHTML=esc(title)+'<span class="heading-dot">.</span>';$('#page-description').textContent=desc;
 $$('.nav-item').forEach(b=>{b.classList.toggle('active',b.dataset.view===state.view);if(b.dataset.view===state.view)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});
 const rows=filtered(); if(!rows.some(c=>c.id===state.selected))state.selected=rows[0]?.id || null; if(Safety.active(current())&&state.tab==='workflow')state.tab='safety';
 renderStats();renderTable();renderRegions();renderActivity();renderDetail();
}
async function refresh(showToast=false) {
 if(refreshing)return; refreshing=true;
 try { if(apiMode) db=await api('/api/municipality/bootstrap'); else {try{const saved=JSON.parse(localStorage.getItem(KEY));if(saved?.fixture_version===1)db=saved;}catch{}}
 error('');render();$('#sync-label').textContent=apiMode?'SQLite 저장 · 8초마다 동기화':'브라우저에 저장 · 가상 데이터';$('#storage-status').innerHTML='<span class="green-dot"></span>'+(apiMode?'SQLite DB 연결 · 가상 데이터':'브라우저 저장 · 가상 데이터');if(showToast)toast('최신 기록을 불러왔어요.');
 } catch(e){error('최신 기록을 불러오지 못했습니다. 서버와 연결을 확인해 주세요.');} finally{refreshing=false;}
}
function applyOffline(c,body) {
 if(c.version!==body.expected_version)throw new Error('변경된 기록을 새로 불러와 주세요.');
 if(c.status==='completed'&&body.action_type!=='note_added'&&!body.action_type.startsWith('safety_'))throw new Error('완료된 업무에는 메모만 추가할 수 있어요.');
 if(Safety.active(c)&&['completed','hospital_requested'].includes(body.action_type))throw new Error('위기 대응 후속 확인을 먼저 마쳐 주세요.');
 const at=new Date().toISOString(),action={id:uid(),case_id:c.id,action_type:body.action_type,actor_name:'김하늘',actor_role:'지자체',note:body.note||'',created_at:at};
 if(body.action_type==='metadata_updated'){Object.assign(c,{assignee:body.assignee,priority:body.priority,due_at:body.due_at});action.note=`담당자 ${c.assignee} · 확인 기한 ${fmt(c.due_at,true)}`;if(c.status==='received')c.status='checking';}
 if(body.action_type==='hospital_requested'){if(!['received','checking'].includes(c.status))throw new Error('이미 요청한 업무입니다.');if(c.assignee==='미배정')throw new Error('담당자를 먼저 배정해 주세요.');c.hospital_request=body.note;c.hospital_requested_at=at;c.status='hospital_requested';}
 if(body.action_type==='contact_logged'){c.last_contact_outcome=body.outcome;c.last_contact_note=body.note;c.last_contact_at=at;action.outcome=body.outcome;if(body.outcome==='connected'&&c.status==='response_received')c.status='resident_informed';}
 if(body.action_type==='completed'){if(c.status!=='resident_informed')throw new Error('주민 안내 후 완료할 수 있어요.');c.status='completed';}
 if(body.action_type.startsWith('safety_')) applyOfflineSafety(c,body,action,at);
 c.version++;c.updated_at=at;db.actions.push(action);persist();
}
async function save(body,clearKey) {
 if(busy)return;const c=current();if(!c||c.status==='unlinked')return;
 busy=true;$$('#detail-panel button[type=submit]').forEach(b=>b.disabled=true);
 try{if(apiMode)await api(`/api/municipality/cases/${encodeURIComponent(c.id)}/actions`,{method:'POST',body:JSON.stringify({...body,expected_version:c.version})});else applyOffline(c,{...body,expected_version:c.version});
 if(clearKey)delete draftFor(c.id)[clearKey];detailCache.clear();await refresh();toast(body.action_type==='hospital_requested'?'병원 대시보드에 확인 요청을 전달했어요.':'처리 내용을 저장했어요.');
 }catch(e){toast(e.message);if(apiMode)await refresh();}finally{busy=false;$$('#detail-panel button[type=submit]').forEach(b=>b.disabled=false);}
}
function setView(view) {state.view=view;state.filter='all';state.search='';state.region='';state.page=0;state.tab=view==='calls'?'transcript':view==='history'?'timeline':view==='safety'?'safety':'workflow';$('#search').value='';$('#sidebar').classList.remove('open');render();}
function selectCase(id) {state.selected=id;if(Safety.active(current())&&state.tab==='workflow')state.tab='safety';renderTable();renderDetail();if(innerWidth<1050)$('#detail-panel').scrollIntoView({behavior:'smooth',block:'start'});}
function openNew(callId) {
 const rows=db.calls.filter(call=>!db.cases.some(c=>c.call_id===call.id));
 $('#new-call').innerHTML=rows.map(c=>`<option value="${esc(c.id)}">${esc(c.name)} · ${esc(fmt(c.started_at,true))} · ${esc(c.purpose)}</option>`).join('');
 $('#new-category').innerHTML=db.categories.map(c=>`<option>${esc(c)}</option>`).join('');
 $('#new-form button[type=submit]').disabled=!rows.length;
 if(!rows.length){toast('모든 통화에 연계 업무가 등록되어 있어요.');return;}
 if(callId&&rows.some(c=>c.id===callId))$('#new-call').value=callId;
 $('#new-request').value=db.calls.find(c=>c.id===$('#new-call').value)?.purpose||'';$('#new-dialog').showModal();
}
function exportCsv() {
 const cell=v=>{let s=String(v??'');if(/^[\s]*[=+@-]/.test(s))s="'"+s;return '"'+s.replace(/"/g,'""')+'"';};
 const lines=[['주민','연락처','지역','지원 유형','요청 내용','담당자','처리 단계','확인 기한'],...filtered().map(c=>[c.call.name,c.call.phone,c.call.region,c.category,c.request_text,c.assignee,stages[c.status],c.due_at||''])];
 const blob=new Blob(['\ufeff'+lines.map(r=>r.map(cell).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='화순군_방문지원_가상업무.csv';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('현재 목록을 CSV로 내보냈어요.');
}
$$('[data-view]').forEach(b=>b.addEventListener('click',()=>setView(b.dataset.view)));
$('.brand').addEventListener('click',()=>setView('overview'));
$('#filters').addEventListener('click',e=>{const b=e.target.closest('[data-filter]');if(b){state.filter=b.dataset.filter;state.page=0;render();}});
$('#search').addEventListener('input',e=>{state.search=e.target.value.trim();state.page=0;render();});
$('#region').addEventListener('change',e=>{state.region=e.target.value;state.page=0;render();});
$('#clear-filters').addEventListener('click',()=>{state.filter='all';state.region='';state.search='';state.page=0;$('#search').value='';render();});
$('#case-table').addEventListener('click',e=>{const r=e.target.closest('[data-case]');if(r)selectCase(r.dataset.case);});
$('#case-table').addEventListener('keydown',e=>{if(['Enter',' '].includes(e.key)){const r=e.target.closest('[data-case]');if(r){e.preventDefault();selectCase(r.dataset.case);}}});
$('#prev').addEventListener('click',()=>{state.page--;renderTable();});$('#next').addEventListener('click',()=>{state.page++;renderTable();});
$('#region-bars').addEventListener('click',e=>{const b=e.target.closest('[data-region]');if(b){state.view='overview';state.region=b.dataset.region;state.filter='all';state.page=0;render();}});
$('#activity-list').addEventListener('click',e=>{const b=e.target.closest('[data-activity-case]');if(b){setView('history');state.selected=b.dataset.activityCase;const idx=filtered().findIndex(c=>c.id===state.selected);state.page=Math.floor(Math.max(0,idx)/7);render();}});
$('#detail-panel').addEventListener('click',async e=>{const tab=e.target.closest('[data-tab]');if(tab){state.tab=tab.dataset.tab;renderDetail();}
 if(e.target.closest('#copy-phone'))try{await navigator.clipboard.writeText(current().call.phone);toast('연락처를 복사했어요.');}catch{toast('복사가 제한되어 있어요. 표시된 번호를 직접 선택해 복사해 주세요.');}
 if(e.target.closest('#retry-detail')){detailCache.delete(`${current().id}:${current().version}`);renderDetail();}
 if(e.target.closest('#register-selected'))openNew(current().call_id);
});
$('#detail-panel').addEventListener('input',e=>{if(e.target.dataset.draft&&current())draftFor(current().id)[e.target.dataset.draft]=e.target.value;});
$('#detail-panel').addEventListener('change',e=>{if(e.target.dataset.draft&&current())draftFor(current().id)[e.target.dataset.draft]=e.target.value;});
$('#detail-panel').addEventListener('keydown',e=>{if(e.target.getAttribute('role')==='tab'&&['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();const tabs=[...$('#detail-panel').querySelectorAll('[data-tab]')],idx=tabs.indexOf(e.target);state.tab=tabs[(idx+(e.key==='ArrowRight'?1:tabs.length-1))%tabs.length].dataset.tab;renderDetail();$(`#detail-tab-${state.tab}`).focus();}});
$('#detail-panel').addEventListener('submit',e=>{e.preventDefault();const f=e.target,values=Object.fromEntries(new FormData(f));if(f.id==='assignment-form'){save({action_type:'metadata_updated',assignee:values.assignee,priority:values.priority,due_at:values.due_at?new Date(values.due_at+'+09:00').toISOString():null});return;}
 const config={'safety-open-form':['safety_opened','safety_report'],'safety-location-form':['safety_updated','safety_update'],'safety-contact-form':['safety_contact_logged','safety_contact'],'safety-handoff-form':['safety_handoff_recorded','safety_handoff'],'safety-close-form':['safety_closed','safety_close'],'hospital-request-form':['hospital_requested','hospital_note'],'contact-form':['contact_logged','contact_note'],'complete-form':['completed','complete_note'],'note-form':['note_added','memo']};
 if(config[f.id]){if(!values.note?.trim()){toast('처리 내용을 입력해 주세요.');return;}save({action_type:config[f.id][0],note:values.note.trim(),...(f.id==='contact-form'?{outcome:values.outcome}:{}),...(f.id.startsWith('safety-')?{situation_type:values.situation_type,location_text:values.location_text||'',contact_target:values.contact_target,contact_result:values.contact_result,resolution:values.resolution}:{})},config[f.id][1]);}
});
function openSafetyGuide(){ $('#safety-guide-content').innerHTML=Safety.guide(db.safety_guidance,esc); $('#safety-guide-dialog').showModal(); }
function applyOfflineSafety(c,b,a,at){
 let s=c.safety,active=Safety.active(c);
 if(b.action_type==='safety_opened'){if(active)throw new Error('진행 중인 대응이 있어요.');a.previous_safety=s;s={status:'checking',category:b.situation_type,report_text:b.note,location_text:b.location_text||'현재 위치 미확인',reported_by:'담당자 직접 접수',opened_at:at};}
 else {if(!active)throw new Error('진행 중인 대응이 없어요.');s={...s};
 if(b.action_type==='safety_updated')Object.assign(s,{location_text:b.location_text||'현재 위치 미확인',latest_situation:b.note});
 if(b.action_type==='safety_contact_logged'){if(!b.contact_target||!b.contact_result)throw new Error('연락 대상과 실제 결과를 선택해 주세요.');Object.assign(s,{last_contact_target:b.contact_target,last_contact_result:b.contact_result,last_contact_note:b.note,last_contact_at:at});a.contact_target=b.contact_target;a.contact_result=b.contact_result;if(b.contact_result==='connected'&&s.status==='checking')s.status='contacted';}
 if(b.action_type==='safety_handoff_recorded'){if(s.last_contact_result!=='connected')throw new Error('연결된 연락 기록이 필요합니다.');Object.assign(s,{status:'handed_over',handoff_target:s.last_contact_target,handoff_note:b.note,handoff_at:at});}
 if(b.action_type==='safety_closed'){if(b.resolution==='handoff_confirmed'&&s.status!=='handed_over')throw new Error('인계 확인을 먼저 기록해 주세요.');if(b.resolution==='safe_confirmed'&&!(s.last_contact_result==='connected'&&['주민 본인','보호자'].includes(s.last_contact_target)))throw new Error('주민·보호자의 직접 확인이 필요합니다.');if(!b.resolution)throw new Error('확인 근거를 선택해 주세요.');Object.assign(s,{status:'closed',resolution:b.resolution,resolution_note:b.note,closed_at:at});}}
 s.updated_at=at;c.safety=s;a.safety_status=s.status;
}
document.addEventListener('click',e=>{if(e.target.closest('[data-open-safety-guide]'))openSafetyGuide();const item=e.target.closest('[data-safety-case]');if(item){setView('safety');state.selected=item.dataset.safetyCase;state.tab='safety';render();if(innerWidth<1050)$('#detail-panel').scrollIntoView({behavior:'smooth'});}const nav=e.target.closest('#safety-board [data-view]');if(nav)setView(nav.dataset.view);});
$('#new-case').addEventListener('click',()=>openNew());$('#new-call').addEventListener('change',()=>{$('#new-request').value=db.calls.find(c=>c.id===$('#new-call').value)?.purpose||'';});
$('#new-form').addEventListener('submit',async e=>{e.preventDefault();if(busy)return;const body={call_id:$('#new-call').value,category:$('#new-category').value,request_text:$('#new-request').value.trim()};if(!body.request_text){toast('주민 요청을 입력해 주세요.');return;}busy=true;const b=e.target.querySelector('button[type=submit]');b.disabled=true;
 try{let result;if(apiMode)result=await api('/api/municipality/cases',{method:'POST',body:JSON.stringify(body)});else{if(db.cases.some(c=>c.call_id===body.call_id))throw new Error('이미 등록된 통화입니다.');const at=new Date().toISOString(),c={id:'case-'+uid(),...body,status:'received',version:1,assignee:'미배정',priority:'normal',due_at:null,hospital_request:'',hospital_response:'',hospital_name:'김장철병원',support:{},created_at:at,updated_at:at,call:db.calls.find(c=>c.id===body.call_id)};db.cases.push(c);db.actions.push({id:uid(),case_id:c.id,actor_name:'김하늘',actor_role:'지자체',action_type:'case_created',note:c.request_text,created_at:at});persist();result={case:c};}
 $('#new-dialog').close();state.view='overview';state.filter='all';state.region='';state.search='';$('#search').value='';state.selected=result.case.id;state.page=0;state.tab='workflow';await refresh();toast('통화에 연계 업무를 등록했어요.');
 }catch(e){toast(e.message);}finally{busy=false;b.disabled=false;}
});
$('#refresh').addEventListener('click',()=>{detailCache.clear();refresh(true);});$('#export').addEventListener('click',exportCsv);
$('#guide-open').addEventListener('click',()=>$('#guide').showModal());$$('[data-close]').forEach(b=>b.addEventListener('click',()=>$(`#${b.dataset.close}`).close()));
$('#menu-toggle').addEventListener('click',()=>$('#sidebar').classList.toggle('open'));
document.addEventListener('keydown',e=>{if(e.key==='Escape')$('#sidebar').classList.remove('open');});
document.addEventListener('click',e=>{if(innerWidth<=700&&!e.target.closest('.sidebar')&&!e.target.closest('#menu-toggle'))$('#sidebar').classList.remove('open');});
if(!apiMode)$('#hospital-link').href='index.html';$('#mode-badge').innerHTML='<i></i>'+(apiMode?'DB LIVE':'DEMO');
persist();await refresh();
setInterval(()=>{if(!document.hidden&&!busy&&!document.activeElement?.matches('input,textarea,select')&&!document.querySelector('dialog[open]'))refresh();},8000);
})();
