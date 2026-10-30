(async()=>{
'use strict';
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const paths={grid:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',phone:'M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 2 .7 2.9a2 2 0 0 1-.5 2.1L8 10a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.5c.9.3 1.9.6 2.9.7a2 2 0 0 1 1.7 2z',pin:'M20 10c0 6-8 12-8 12S4 16 4 10a8 8 0 1 1 16 0z M15 10a3 3 0 1 1-6 0 3 3 0 0 1 6 0',note:'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z M14 2v6h6 M8 13h8 M8 17h6',building:'M6 22V3h12v19 M3 22h18 M9 6h1 M14 6h1 M9 10h1 M14 10h1 M9 14h1 M14 14h1 M10 22v-4h4v4',sparkles:'m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5z M20 2v4 M18 4h4',home:'m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z',bell:'M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9 M10 21h4',plus:'M12 5v14 M5 12h14',download:'M12 3v12 m-5-5 5 5 5-5 M4 16v5h16v-5',search:'M21 21l-5-5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0','chevron-right':'m9 5 7 7-7 7','chevron-left':'m15 5-7 7 7 7',activity:'M2 12h4l3-8 6 16 3-8h4',check:'m5 12 4 4L19 6','check-circle':'M22 11v1a10 10 0 1 1-6-9 M22 4 12 14l-3-3',clock:'M12 8v5l3 2 M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',shield:'M12 2 3 6v6c0 5 9 10 9 10s9-5 9-10V6z m-4 10 3 3 5-5',x:'m6 6 12 12 M6 18 18 6',menu:'M3 6h18 M3 12h18 M3 18h18','arrow-right':'M4 12h16 m-6-6 6 6-6 6',users:'M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2 M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0 M23 21v-2a4 4 0 0 0-3-3.9 M16 3.1a4 4 0 0 1 0 7.8',inbox:'M4 3h16l2 12v6H2v-6z M2 15h6l2 3h4l2-3h6',message:'M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8z'};
paths.copy='M9 9h12v12H9z M5 15H3V3h12v2';
const icon=name=>`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name]||paths.note}"/></svg>`;
$$('[data-icon]').forEach(e=>e.innerHTML=icon(e.dataset.icon));
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const TODAY='2026-10-30', KEY='kjc-hospital-desk-v1';
const time=t=>new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(t));
const date=t=>new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(t));
const dateLabel=t=>new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',month:'long',day:'numeric'}).format(new Date(t));
const stamp=(d,t)=>new Date(`${d}T${t}:00+09:00`).toISOString();
const sessionStart=Date.now(), demoEpoch=Date.parse(`${TODAY}T13:45:00+09:00`);
const now=()=>new Date(demoEpoch+Date.now()-sessionStart).toISOString();
const uid=()=>globalThis.crypto?.randomUUID?.()||`id-${Date.now()}-${Math.random().toString(36).slice(2)}`;
const reviewLabels={unreviewed:'미확인',reviewing:'확인 중',reviewed:'확인 완료'};
const views={dashboard:['대시보드','방문 준비 데스크','어르신의 전화 한 통부터 병원 방문까지, 준비 상황을 한눈에 확인하세요.'],history:['통화 기록','전화 한 통, 이어지는 기록','이전 상담의 준비 안내와 직원 확인 내역을 날짜별로 살펴보세요.'],locations:['위치 현황','통화가 시작된 곳','각 통화 당시의 시연용 위치를 확인하세요. 실제 GPS 위치가 아닙니다.'],notes:['직원 메모','다음 담당자에게 이어지는 메모','통화에 남긴 전달사항과 확인 내역을 함께 살펴보세요.']};
function seed(){
 const people=[['임정숙',76,'화순군 동면','정기 외래 방문','재진','13:42','in_progress','unreviewed','mint'],['박정호',81,'화순군 한천면','방문 절차 문의','초진','13:39','in_progress','unreviewed','blue'],['이정례',82,'화순군 화순읍','정기 외래 방문','재진','13:31','ended','unreviewed','peach'],['김영숙',78,'화순군 능주면','준비사항 확인','재진','13:24','ended','reviewing','violet'],['최복순',74,'화순군 도곡면','첫 방문 안내','초진','13:15','ended','reviewed','mint'],['정만수',79,'화순군 이양면','접수 순서 문의','재진','12:56','ended','unreviewed','blue'],['박순임',83,'화순군 화순읍','방문 일정 확인','재진','12:38','ended','reviewed','peach'],['강태식',77,'위치 미확인','첫 방문 안내','초진','12:21','ended','reviewing','violet'],['오정숙',75,'화순군 동복면','준비사항 확인','재진','11:48','ended','reviewed','mint'],['윤영수',80,'화순군 사평면','방문 절차 문의','초진','11:25','ended','unreviewed','blue'],['장명자',72,'화순군 춘양면','정기 외래 방문','재진','10:46','ended','reviewed','peach'],['한덕수',85,'위치 미확인','연결 중 종료','미확인','10:12','failed','unreviewed','violet']];
 const positions=[[35.086,127.026],[35.006,127.001],[35.061,126.992],[35.001,126.947],[35.025,126.914],[34.983,126.994],[35.069,126.981],null,[35.124,127.062],[35.019,127.048],[34.996,126.953],null];
 const db={schema_version:1,calls:[],call_locations:[],call_messages:[],call_events:[],desk_actions:[]};
 people.forEach((p,i)=>{
  const id=`demo-call-${String(i+1).padStart(3,'0')}`,started=stamp(TODAY,p[5]);
  const c={id,caller_key:`demo-caller-${i+1}`,name:p[0],age:p[1],region:p[2],purpose:p[3],visit_type:p[4],started_at:started,ended_at:p[6]==='in_progress'?null:new Date(Date.parse(started)+123000).toISOString(),call_status:p[6],review_status:p[7],color:p[8],phone:`010-0000-${String(1001+i)}`,hospital_code:'KJC_DEMO',planned_visit_text:i===2?'10월 31일 오후 방문 예정':'방문 전 안내 확인',summary_status:p[6]==='ended'?'ready':'pending',summary_text:p[6]==='ended'?`${p[0]} 님은 ${p[3]}을 위해 전화했습니다. ${p[4]==='초진'?'김장철병원에 처음 방문할 예정으로, 접수 장소와 방문 순서에 대한 안내가 필요합니다.':'기존에 다니던 병원에 방문할 예정으로, 받은 안내와 준비사항을 다시 확인하고 싶어 합니다.'}${i===2?' 혼자 방문할 예정이며, 도착 후 먼저 할 일을 짧게 설명해 달라고 요청했습니다.':''}`:'',data_mode:'simulated',ai_mode:null,summary_source:'fixture',version:1};
  db.calls.push(c);
  if(positions[i])db.call_locations.push({id:`loc-${id}`,call_id:id,latitude:positions[i][0],longitude:positions[i][1],location_source:'simulated',observed_at:started,accuracy_m:null});
  const texts=[['assistant','안녕하세요. 김장철병원 방문 준비를 도와드리는 AI 안내입니다. 어떤 준비를 도와드릴까요?'],['caller',`${p[0]}인데요. 병원에 가기 전에 ${p[3]}에 대해 좀 알고 싶어서요.`],['assistant','네, 하나씩 함께 확인해 볼게요. 저희 병원에는 처음 방문하시나요?'],['caller',p[4]==='초진'?'네, 처음 가는 거라 어디로 가야 할지 모르겠어요.':'아니요, 전에 다녔어요. 이번에는 혼자 가려고 해요.'],['assistant','병원에서 미리 받으신 안내가 있다면 확인해 주세요. 방문 전에 준비할 것과 도착 후 확인할 순서를 차례로 정리해 드릴게요.']];
  const count=p[6]==='failed'?1:p[6]==='in_progress'?3:5;
  texts.slice(0,count).forEach(([speaker,text],j)=>db.call_messages.push({id:`msg-${id}-${j}`,call_id:id,sequence:j+1,speaker,text,occurred_at:new Date(Date.parse(started)+j*14000).toISOString()}));
  db.call_events.push({id:`evt-${id}`,source:'fixture',event_id:`started-${id}`,call_id:id,event_type:'call_started',occurred_at:started});
 });
 for(const [n,day,at,lat,lng] of [[1,'2026-10-23','10:32',35.041,126.974],[2,'2026-10-09','14:16',35.061,126.992],[3,'2026-10-26','11:20',35.002,126.947]]){
  const original=db.calls[n===3?3:2],id=`past-call-${n}`;
  db.calls.push({...original,id,started_at:stamp(day,at),ended_at:stamp(day,`${at.slice(0,3)}${String(Number(at.slice(3))+2).padStart(2,'0')}`),call_status:'ended',review_status:'reviewed',summary_text:n===1?'정기 외래 방문 전 접수 순서와 병원에서 받은 안내를 확인한 시연 기록입니다.':'이전 방문 전 준비사항을 다시 설명하고 확인한 시연 기록입니다.',summary_status:'ready',planned_visit_text:'이전 방문 준비 상담'});
  db.call_locations.push({id:`loc-${id}`,call_id:id,latitude:lat,longitude:lng,location_source:'simulated',observed_at:stamp(day,at),accuracy_m:null});
  db.call_messages.push({id:`msg-${id}`,call_id:id,sequence:1,speaker:'caller',text:'병원 가기 전에 준비할 것을 다시 확인하려고 전화했어요.',occurred_at:stamp(day,at)});
 }
 const notes=[['demo-call-005','첫 방문 안내를 확인했습니다. 추가로 궁금한 점은 현장 접수처에서 문의하도록 안내합니다.','13:26'],['demo-call-007','기존 안내 내용을 다시 확인했습니다. 다음 담당자도 방문 준비 요약을 확인해 주세요.','13:11'],['demo-call-004','혼자 방문할 예정입니다. 도착 후 접수 순서를 다시 설명해 주세요.','13:29'],['past-call-1','방문 전 안내 내용을 천천히 반복해서 확인했습니다.','10:42']];
 notes.forEach(([call_id,note,t],i)=>db.desk_actions.push({id:`action-note-${i}`,call_id,actor_key:'demo-staff-01',actor_name:'이지은',action_type:'note_added',note,created_at:stamp(call_id.startsWith('past')?'2026-10-23':TODAY,t)}));
 for(const [i,id]of ['demo-call-005','demo-call-007','demo-call-009','demo-call-011'].entries())db.desk_actions.push({id:`review-seed-${i}`,call_id:id,actor_name:i%2?'김수현':'이지은',actor_key:`demo-staff-0${i%2+1}`,action_type:'review_changed',previous_status:'reviewing',new_status:'reviewed',created_at:stamp(TODAY,['13:28','13:14','12:08','11:02'][i])});
 return db;
}
const apiMode=document.querySelector('meta[name="hospital-data-source"]')?.content==='api';
async function api(path,options={}){const r=await fetch(path,{...options,headers:{'Content-Type':'application/json',...options.headers}});if(!r.ok){const data=await r.json().catch(()=>({}));throw new Error(typeof data.detail==='string'?data.detail:`API 오류 ${r.status}`);}return r.json();}
let db;if(apiMode){try{db=await api('/api/team/bootstrap');}catch(error){$('#stats').innerHTML='<div class="backend-error">DB에 연결하지 못했습니다. 서버를 확인한 뒤 새로고침해 주세요.</div>';$('#start-demo').disabled=true;return;}}else try{const saved=JSON.parse(localStorage.getItem(KEY));db=saved?.schema_version===1&&Array.isArray(saved.calls)&&saved.calls.length?saved:seed();}catch{db=seed();}
// Explicit fixture links: never derive a patient identity from a phone number.
if(db.chart_fixture_version!==2||!Array.isArray(db.patients)){db.patients=window.HospitalPatientCharts.createProfiles();db.chart_fixture_version=2;}
const chartLinks=Object.fromEntries(Array.from({length:12},(_,i)=>[`demo-call-${String(i+1).padStart(3,'0')}`,`demo-patient-${String(i+1).padStart(3,'0')}`]));
Object.assign(chartLinks,{'past-call-1':'demo-patient-003','past-call-2':'demo-patient-003','past-call-3':'demo-patient-004'});
for(const c of db.calls){if(!c.patient_id&&chartLinks[c.id])c.patient_id=chartLinks[c.id];}
const patientFor=c=>db.patients.find(p=>p.id===c.patient_id);
let state={view:'dashboard',filter:'all',search:'',selected:'demo-call-003',detailTab:'preparation',page:0,date:'',zoom:1,playing:false};
const drafts=new Map(),preparationCache=new Map(),preparationQueries=new Map();let timers=[],toastTimer;
function persist(){if(apiMode)return true;try{localStorage.setItem(KEY,JSON.stringify(db));return true;}catch{toast('브라우저 저장 공간을 사용할 수 없어 현재 화면에만 반영했어요.');return false;}}
const todayCalls=()=>db.calls.filter(c=>date(c.started_at)===TODAY);
const callById=id=>db.calls.find(c=>c.id===id);
const selected=()=>callById(state.selected);
const latestLocation=id=>db.call_locations.filter(l=>l.call_id===id).sort((a,b)=>b.observed_at.localeCompare(a.observed_at))[0];
const actionsFor=id=>db.desk_actions.filter(a=>a.call_id===id).sort((a,b)=>b.created_at.localeCompare(a.created_at));
function filtered(){return db.calls.filter(c=>{
 if(state.view==='dashboard'&&date(c.started_at)!==TODAY)return false;
 if(state.date&&date(c.started_at)!==state.date)return false;
 if(state.filter==='active'&&c.call_status!=='in_progress')return false;
 if(state.filter==='pending'&&c.review_status==='reviewed')return false;
 if(state.filter==='reviewed'&&c.review_status!=='reviewed')return false;
 const phoneQuery=state.search.replace(/\D/g,'');
 return !state.search||`${c.name} ${c.region} ${c.purpose} ${c.phone||''} ${c.id}`.includes(state.search)||(phoneQuery.length>=3&&String(c.phone||'').replace(/\D/g,'').includes(phoneQuery));
 }).sort((a,b)=>b.started_at.localeCompare(a.started_at)||b.id.localeCompare(a.id));}
function statusTag(c){return c.call_status==='in_progress'?'<span class="tag violet"><span class="wave"><i></i><i></i><i></i><i></i></span>통화 중</span>':c.call_status==='failed'?'<span class="tag amber">연결 종료</span>':'<span class="tag neutral">통화 종료</span>';}
function reviewTag(c){return `<span class="tag ${c.review_status==='reviewed'?'green':c.review_status==='reviewing'?'violet':'amber'}">${c.review_status==='reviewed'?'<i></i>':''}${reviewLabels[c.review_status]}</span>`;}
const contactNumber=c=>{const value=String(c.phone||'').trim(),digits=value.replace(/\D/g,'');return digits.length===11?digits.replace(/^(\d{3})(\d{4})(\d{4})$/,'$1-$2-$3'):value;};
async function copyContact(){const number=contactNumber(selected());if(!number)return;try{if(!navigator.clipboard?.writeText)throw new Error('clipboard unavailable');await navigator.clipboard.writeText(number);toast(`연락처 ${number}를 복사했어요.`);}catch{toast('번호 복사가 제한돼 있어요. 표시된 번호를 직접 선택해 복사해 주세요.');}}
function renderStats(){
 const a=todayCalls(),active=a.filter(c=>c.call_status==='in_progress').length,pending=a.filter(c=>c.review_status!=='reviewed').length,done=a.length-pending;
 const cards=[['오늘의 통화',a.length,'phone','violet','전체 방문 준비 상담',`오늘 ${a.length}건 연결`],['통화 중',active,'activity','violet','대화가 이어지고 있어요',active?'상담 진행 중':'진행 중인 통화 없음'],['직원 확인 필요',pending,'inbox','amber','한 번 더 살펴봐 주세요',`미확인·확인 중 ${pending}건`],['확인 완료',done,'check-circle','green','확인 내역이 기록됐어요',`전체 통화의 ${Math.round(done/a.length*100)||0}%`]];
 $('#stats').innerHTML=cards.map(([label,n,ic,color,desc,foot])=>`<article class="stat"><div class="stat-head"><span>${label}</span><span class="stat-icon ${color}">${icon(ic)}</span></div><div class="stat-number">${n}<small>건</small></div><div class="stat-bottom"><strong class="${color==='amber'?'amber-text':color==='green'?'green-text':''}">${foot}</strong><span>· ${desc}</span></div><div class="mini-bars" aria-hidden="true">${[7,12,9,17,13,19,22].map(h=>`<i style="height:${h}px"></i>`).join('')}</div></article>`).join('');
 $('#nav-call-count').textContent=db.calls.length;$('#nav-note-count').textContent=db.desk_actions.filter(a=>a.action_type==='note_added').length;$('#active-count').textContent=active;
 $('#notification-dot').hidden=!pending;
}
function renderTable(){
 const rows=filtered(),pageSize=7;state.page=Math.max(0,Math.min(state.page,Math.ceil(rows.length/pageSize)-1));
 const page=rows.slice(state.page*pageSize,(state.page+1)*pageSize);
 $('#list-title').innerHTML=`${state.view==='history'?'통화 기록':'오늘의 통화'} <span class="count-badge">${rows.length}</span>`;
 $('#list-description').textContent=state.view==='history'?'현재 통화와 이전 방문 준비 상담을 함께 확인하세요.':'방문 준비 상담과 직원 확인 현황';
 $('#call-table').innerHTML=page.map(c=>`<tr data-call-id="${c.id}" tabindex="0" aria-label="${esc(c.name)} 통화 상세 보기" aria-selected="${state.selected===c.id}" class="${state.selected===c.id?'selected':''}"><td><div class="person-cell"><span class="person-avatar ${c.color}">${esc(c.name[0])}</span><span><strong>${esc(c.name)} <span class="faded" style="font-size:8px;font-weight:400">${c.age}세</span></strong><small>${esc(c.region)}</small></span></div></td><td class="contact-cell">${esc(contactNumber(c)||'미확인')}</td><td class="purpose-cell"><strong>${esc(c.purpose)}</strong><small>${esc(c.visit_type)} · 방문 준비</small></td><td class="time-cell">${state.view==='history'?`<small style="display:block;font-size:8px;margin-bottom:4px">${dateLabel(c.started_at)}</small>`:''}${time(c.started_at)}</td><td>${statusTag(c)}</td><td>${reviewTag(c)}</td><td>${icon('chevron-right')}</td></tr>`).join('')||`<tr class="empty-row"><td colspan="7"><div class="empty-state">${icon('search')}조건에 맞는 통화가 없어요.<br>다른 이름이나 필터로 다시 찾아보세요.</div></td></tr>`;
 $('#page-range').textContent=rows.length?`${state.page*pageSize+1}–${Math.min((state.page+1)*pageSize,rows.length)} / ${rows.length}건`:'0건';
 $('#prev-page').disabled=state.page===0;$('#next-page').disabled=(state.page+1)*pageSize>=rows.length;
 $('#history-tools').hidden=state.view!=='history';$('#date-filter').value=state.date;
 $$('#status-tabs button').forEach(b=>{b.classList.toggle('selected',b.dataset.filter===state.filter);b.setAttribute('aria-pressed',String(b.dataset.filter===state.filter));});
}
function summaryContent(c){
 return `<div class="summary-label"><strong>${icon('sparkles')}방문 준비 요약</strong><small>시연용 요약</small></div><div class="summary-box ${c.summary_status!=='ready'?'pending':''}">${c.summary_status==='ready'?esc(c.summary_text):c.call_status==='in_progress'?'어르신과 대화하고 있어요. 통화가 끝나면 방문 준비 요약이 여기에 표시됩니다.':c.call_status==='failed'?'연결이 종료되어 요약을 만들지 못했어요. 남아 있는 대화를 확인해 주세요.':'통화는 종료됐어요. 준비 요약을 정리하고 있어요.'}</div><h4 class="detail-subtitle">${icon('check-circle')}함께 확인할 내용</h4><ul class="checklist"><li>${icon('check')}병원에서 미리 받은 안내가 있는지 확인</li><li>${icon('check')}방문 전 준비할 것과 문의할 내용 정리</li><li>${icon('check')}도착 후 접수 순서를 다시 확인</li></ul><p class="summary-footnote">가상 대화를 바탕으로 작성한 시연용 안내입니다.<br>실제 예약·접수 또는 의료적 판단을 의미하지 않습니다.</p>`;
}
function transcriptContent(c){
 const m=db.call_messages.filter(m=>m.call_id===c.id).sort((a,b)=>a.sequence-b.sequence);
 return `<div class="summary-label"><strong>${icon('message')}상담 대화</strong>${c.call_status==='in_progress'?'<span class="live-indicator"><span class="status-dot"></span>대화 중</span>':`<small>발화 ${m.length}개</small>`}</div><div class="transcript">${m.map(m=>`<div class="message ${m.speaker}"><small>${m.speaker==='assistant'?'AI 안내':esc(c.name)+' 님'} · ${time(m.occurred_at)}</small><div class="bubble">${esc(m.text)}</div></div>`).join('')||'<div class="empty-state">아직 저장된 대화가 없어요.</div>'}</div>`;
}
function historyContent(c){
 const a=db.calls.filter(x=>x.caller_key===c.caller_key&&x.id!==c.id&&x.started_at<c.started_at).sort((a,b)=>b.started_at.localeCompare(a.started_at));
 return `<div class="summary-label"><strong>${icon('clock')}이전 통화 ${a.length}건</strong><small>동일 발신자 식별값</small></div><p class="history-current">현재 통화보다 이전에 기록된 상담입니다.<br>위치는 각 통화 당시의 가상 좌표로 보관돼요.</p>${a.map(x=>{const l=latestLocation(x.id);return `<article class="history-card"><header><strong>${dateLabel(x.started_at)} ${time(x.started_at)}</strong>${reviewTag(x)}</header><p>${esc(x.summary_text||'요약이 없는 상담입니다.')}</p><div class="history-location">${icon('pin')}${l?`${l.latitude.toFixed(4)}, ${l.longitude.toFixed(4)} · 가상 위치`:'위치 미확인'}</div><button class="text-link" data-history-id="${x.id}">이 통화 자세히 보기 →</button></article>`;}).join('')||`<div class="empty-state">${icon('clock')}이전에 저장된 통화가 없어요.</div>`}`;
}
function preparationKey(c){return `${c.id}:${c.version}:${patientFor(c)?.chart_import_id||patientFor(c)?.as_of||'unlinked'}:${db.preparation_revision||''}:${preparationQueries.get(c.id)||''}`;}
function preparationContent(c){
 const query=preparationQueries.get(c.id)||'';
 if(!apiMode){const fixture=window.HospitalPreparationFixtures?.[c.id];return window.HospitalPreparation.render(fixture,query,fixture?'ready':'unavailable',false);}
 const key=preparationKey(c),cached=preparationCache.get(key);
 if(cached)return window.HospitalPreparation.render(cached.data,query,cached.status);
 preparationCache.set(key,{status:'loading'});
 api(`/api/team/calls/${encodeURIComponent(c.id)}/preparation?q=${encodeURIComponent(query)}`).then(data=>{
 preparationCache.set(key,{status:'ready',data});
 if(selected()?.id===c.id&&preparationKey(selected())===key&&state.detailTab==='preparation')renderDetail();
 }).catch(()=>{preparationCache.set(key,{status:'error'});if(selected()?.id===c.id&&preparationKey(selected())===key&&state.detailTab==='preparation')renderDetail();});
 return window.HospitalPreparation.render(null,query,'loading');
}
function renderDetail(){
 const c=selected();if(!c)return;
 const focusedPrep=document.activeElement?.id==='preparation-query'?{value:document.activeElement.value,start:document.activeElement.selectionStart,end:document.activeElement.selectionEnd}:null;
 const focusedNote=document.activeElement?.id==='note-input'?{start:document.activeElement.selectionStart,end:document.activeElement.selectionEnd}:null;
 const loc=latestLocation(c.id),notes=actionsFor(c.id).filter(a=>a.action_type==='note_added'),draft=drafts.get(c.id)||'';
 $('#detail-panel').innerHTML=`<div class="detail-heading"><h2>환자 정보 · 통화 상세</h2><span class="reference">${esc(c.id.replace('demo-call-','CALL #').replace('past-call-','PAST #'))}</span></div>${state.playing&&c.id.startsWith('sim-')&&c.call_status==='in_progress'?'<div class="simulation-banner"><span class="wave"><i></i><i></i><i></i></span>가상 통화 시나리오가 진행 중이에요.</div>':''}<div class="detail-person"><span class="detail-avatar">${esc(c.name[0])}</span><div><h3>${esc(c.name)}<small>${c.age}세</small></h3><div class="detail-contact"><span><small>연락처</small><strong>${esc(contactNumber(c)||'연락처 미확인')}</strong></span><button class="icon-button contact-copy" id="copy-contact" aria-label="연락처 복사" title="연락처 복사" ${!contactNumber(c)?'disabled':''}>${icon('copy')}</button></div></div>${statusTag(c)}</div><div class="detail-facts"><div class="detail-fact"><label>방문 목적</label><span>${esc(c.purpose)}</span></div><div class="detail-fact"><label>방문 구분</label><span>${esc(c.visit_type)}</span></div><div class="detail-fact"><label>연결 시각</label><span>${dateLabel(c.started_at)} ${time(c.started_at)}</span></div><div class="detail-fact"><label>방문 계획</label><span>${esc(c.planned_visit_text)}</span></div></div><div class="detail-tabs" role="tablist" aria-label="통화 상세 항목">${[['preparation','방문 준비'],['chart','환자 차트'],['summary','AI 요약'],['transcript','대화 내역'],['history','과거 기록']].map(([v,l])=>`<button role="tab" aria-selected="${state.detailTab===v}" aria-controls="detail-tab-content" id="tab-${v}" class="${state.detailTab===v?'active':''}" data-detail-tab="${v}">${l}</button>`).join('')}</div><div class="detail-tab-content" id="detail-tab-content" role="tabpanel" aria-labelledby="tab-${state.detailTab}">${state.detailTab==='preparation'?preparationContent(c):state.detailTab==='chart'?window.HospitalPatientCharts.render(patientFor(c),state.chartMetric||'blood_pressure'):state.detailTab==='summary'?summaryContent(c):state.detailTab==='transcript'?transcriptContent(c):historyContent(c)}<div class="location-strip">${icon('pin')}<span>${esc(c.region)}<small>${loc?`통화 당시 · ${loc.latitude.toFixed(4)}, ${loc.longitude.toFixed(4)}`:'좌표가 확인되지 않았어요.'}</small></span><span class="tag ${loc?'green':'neutral'}">${loc?'가상 위치':'미확인'}</span></div></div><div class="review-section"><div class="section-heading"><h3>직원 확인</h3><select class="review-select" id="review-select" aria-label="직원 확인 상태">${Object.entries(reviewLabels).map(([v,l])=>`<option value="${v}" ${c.review_status===v?'selected':''}>${l}</option>`).join('')}</select></div>${notes.slice(0,2).map(n=>`<div class="existing-note"><p>${esc(n.note)}</p><small><span class="note-author">${esc(n.actor_name)}</span> · ${dateLabel(n.created_at)} ${time(n.created_at)}</small></div>`).join('')}<label for="note-input" class="note-label">다음 담당자에게 남길 메모</label><textarea id="note-input" class="note-input" maxlength="800" placeholder="추가로 확인할 내용이나 전달사항을 남겨주세요.">${esc(draft)}</textarea><div class="note-bottom"><small id="note-counter">${draft.length} / 800</small><button class="button primary note-save" id="save-note" ${!draft.trim()?'disabled':''}>${icon('check')}메모 저장</button></div></div><div class="detail-footer">${icon('shield')}직원 확인은 실제 진료 접수와 별개입니다.</div>`;
 if(state.detailTab==='transcript'&&state.playing&&c.id.startsWith('sim-')){const t=$('.transcript');if(t)t.scrollTop=t.scrollHeight;}
 if(focusedPrep&&$('#preparation-query')){const input=$('#preparation-query');input.value=focusedPrep.value;input.focus({preventScroll:true});input.setSelectionRange(focusedPrep.start,focusedPrep.end);}
 if(focusedNote){const input=$('#note-input');input.focus({preventScroll:true});input.setSelectionRange(focusedNote.start,focusedNote.end);}
}
function renderMap(){
 const calls=(state.view==='history'?filtered():todayCalls()),points=calls.map(c=>({c,l:latestLocation(c.id)})).filter(x=>x.l);
 const pos=l=>[35+(l.longitude-126.88)/.22*470,220-(l.latitude-34.96)/.20*195];
 const svg=`<svg viewBox="0 0 540 240" role="img" aria-label="화순 지역 통화 위치를 보여주는 시연용 개략도"><defs><pattern id="map-grid" width="50" height="50" patternUnits="userSpaceOnUse"><path d="M50 0H0V50" fill="none" stroke="#eaf0e7" stroke-width="1"/></pattern><filter id="pin-shadow"><feDropShadow dx="0" dy="2" stdDeviation="2" flood-color="#78816f" flood-opacity=".2"/></filter></defs><g transform="translate(270 120) scale(${state.zoom}) translate(-270 -120)"><rect width="540" height="240" fill="#f3f6f0"/><rect width="540" height="240" fill="url(#map-grid)"/><path d="M0 55Q65 5 122 56T260 54T400 30T540 53V0H0Z M0 180Q50 150 90 190T210 201T360 180T540 205V240H0Z" fill="#e9f0e3"/><path d="M460-20Q390 50 410 90T360 155T342 260" fill="none" stroke="#dcebe9" stroke-width="14"/><g fill="none" stroke="#fff" stroke-width="10"><path d="M-20 161Q92 140 167 155T328 105T560 83"/><path d="M100-20Q160 80 207 115T325 260"/><path d="M-10 62Q120 95 245 77T520 170"/><path d="M339-10 300 98 198 250"/></g><g fill="none" stroke="#e9e8df" stroke-width="1.5"><path d="M-20 161Q92 140 167 155T328 105T560 83"/><path d="M100-20Q160 80 207 115T325 260"/><path d="M-10 62Q120 95 245 77T520 170"/><path d="M339-10 300 98 198 250"/></g><g fill="#acb4a8" font-size="11" font-family="sans-serif"><text x="231" y="88">화순읍</text><text x="404" y="59">동면</text><text x="136" y="191">능주면</text><text x="382" y="185">한천면</text><text x="75" y="91">도곡면</text></g>${points.map(({c,l})=>{const[x,y]=pos(l),sel=state.selected===c.id;return `<g class="map-marker" data-map-id="${c.id}" role="button" tabindex="0" aria-label="${esc(c.name)} 님의 가상 통화 위치"><title>${esc(c.name)} · ${esc(c.region)} · 가상 위치</title>${sel?`<circle cx="${x}" cy="${y}" r="15" fill="#53a88e" opacity=".13"/>`:''}<circle cx="${x}" cy="${y}" r="${sel?7:5}" fill="${sel?'#409e83':'#a78acd'}" stroke="white" stroke-width="2" filter="url(#pin-shadow)"/></g>`;}).join('')}<g transform="translate(260 120)"><rect x="-10" y="-10" width="21" height="21" rx="6" fill="white" stroke="#c3b1e0"/><path d="M0-5v10 M-5 0h10" stroke="#8d6bbf" stroke-width="3"/><rect x="-37" y="16" width="78" height="17" rx="4" fill="white" fill-opacity=".94"/><text x="2" y="28" text-anchor="middle" font-family="sans-serif" font-size="9" fill="#9a89b0">김장철병원 · 가상</text></g></g></svg>`;
 $('#map-canvas').innerHTML=svg+'<div class="map-controls"><button data-zoom="in" aria-label="지도 확대">+</button><button data-zoom="out" aria-label="지도 축소">−</button></div><span class="map-hint">시연 개략도 · 실제 도로·병원 위치와 다릅니다.</span>';
 $('#map-count').textContent=`위치 확인 ${points.length}건`;
}
function activityText(a){const c=callById(a.call_id);return a.action_type==='note_added'?`<strong>${esc(a.actor_name)}</strong> 님이 <strong>${esc(c?.name||'상담자')}</strong> 님의<br>상담에 메모를 남겼어요.`:`<strong>${esc(a.actor_name)}</strong> 님이 <strong>${esc(c?.name||'상담자')}</strong> 님의<br>상담을 <strong>${reviewLabels[a.new_status]}</strong> 상태로 변경했어요.`;}
function renderActivities(){
 const a=[...db.desk_actions].sort((a,b)=>b.created_at.localeCompare(a.created_at));
 $('#activity-list').innerHTML=a.slice(0,4).map(a=>`<div class="activity-item"><span class="activity-avatar">${esc(a.actor_name.slice(1,2))}</span><div class="activity-copy"><p>${activityText(a)}</p><small>${dateLabel(a.created_at)} ${time(a.created_at)}</small></div></div>`).join('')||'<div class="empty-state">아직 처리 활동이 없어요.</div>';
 const notes=a.filter(a=>a.action_type==='note_added');$('#all-notes-count').textContent=`${notes.length}개의 메모`;
 $('#all-notes-list').innerHTML=notes.map(a=>{const c=callById(a.call_id);return `<article class="all-note-row" tabindex="0" data-note-call="${a.call_id}" role="button" aria-label="${esc(c?.name)} 님의 통화 열기"><span class="person-avatar ${c?.color||''}">${esc(c?.name[0])}</span><div class="all-note-copy"><header><strong>${esc(c?.name)} <span class="muted" style="font-size:10px;font-weight:400">${esc(c?.purpose)}</span></strong><small>${dateLabel(a.created_at)} ${time(a.created_at)}</small></header><p>${esc(a.note)}</p><small>${esc(a.actor_name)} · 원무팀</small></div></article>`;}).join('')||'<div class="empty-state">아직 저장된 직원 메모가 없어요.</div>';
}
function renderNotifications(){const rows=todayCalls().filter(c=>c.review_status!=='reviewed').sort((a,b)=>b.started_at.localeCompare(a.started_at));$('#notification-panel').innerHTML=`<strong>직원 확인이 필요한 통화 <span class="count-badge">${rows.length}</span></strong>${rows.slice(0,5).map(c=>`<button class="notification-row" data-notification-id="${c.id}"><span><strong>${esc(c.name)}</strong><small>${esc(c.purpose)} · ${time(c.started_at)}</small></span>${reviewTag(c)}</button>`).join('')||'<div class="empty-state">모든 통화를 확인했어요.</div>'}`;}
function render(){
 const v=views[state.view];$('#breadcrumb-current').textContent=v[0];$('#page-title').textContent=v[1];$('#page-description').textContent=v[2];
 document.body.classList.toggle('locations-view',state.view==='locations');document.body.classList.toggle('notes-view',state.view==='notes');$('#all-notes-panel').hidden=state.view!=='notes';
 $$('.nav-item').forEach(b=>{b.classList.toggle('active',b.dataset.view===state.view);if(b.dataset.view===state.view)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});
 renderStats();renderTable();renderMap();renderDetail();renderActivities();renderNotifications();
 $('#start-demo').disabled=state.playing;$('#start-demo').innerHTML=state.playing?'<span class="wave"><i></i><i></i><i></i></span>시연 통화 진행 중':icon('plus')+'새 통화 시연';
}
function toast(t){$('#toast').textContent=t;$('#toast').classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').classList.remove('show'),3500);}
function selectCall(id,reveal=false){if(!callById(id))return;state.selected=id;state.detailTab='preparation';if(reveal){state.filter='all';state.search='';$('#search-input').value='';state.date='';if(date(callById(id).started_at)!==TODAY)state.view='history';const idx=filtered().findIndex(c=>c.id===id);state.page=Math.floor(Math.max(0,idx)/7);}render();if(innerWidth<1000)$('#detail-panel').scrollIntoView({behavior:'smooth',block:'start'});}
function setView(v){state.view=v;state.filter='all';state.search='';state.date='';state.page=0;$('#search-input').value='';$('#sidebar').classList.remove('open');render();}
async function saveNote(){const c=selected(),input=$('#note-input'),text=input.value.trim();if(!text)return;if(apiMode){const button=$('#save-note');button.disabled=true;try{await api(`/api/team/calls/${encodeURIComponent(c.id)}/desk-actions`,{method:'POST',body:JSON.stringify({action_type:'note_added',note:text,expected_version:c.version})});drafts.delete(c.id);await refreshDatabase();toast('메모를 SQLite DB에 저장했어요.');}catch(error){toast(error.message);await refreshDatabase().catch(()=>{});}finally{if($('#save-note'))$('#save-note').disabled=!($('#note-input')?.value.trim());}return;}db.desk_actions.push({id:uid(),call_id:c.id,actor_key:'demo-staff-01',actor_name:'이지은',action_type:'note_added',note:text,created_at:now()});c.version++;drafts.delete(c.id);const saved=persist();render();if(saved)toast('메모를 저장했어요. 과거 기록에서도 확인할 수 있어요.');}
async function changeReview(value){const c=selected();if(!Object.hasOwn(reviewLabels,value)||c.review_status===value)return;if(apiMode){try{await api(`/api/team/calls/${encodeURIComponent(c.id)}/desk-actions`,{method:'POST',body:JSON.stringify({action_type:'review_changed',new_status:value,expected_version:c.version})});await refreshDatabase();toast('직원 확인 상태를 SQLite DB에 저장했어요.');}catch(error){toast(error.message);await refreshDatabase().catch(()=>{});}return;}const old=c.review_status;c.review_status=value;c.version++;db.desk_actions.push({id:uid(),call_id:c.id,actor_key:'demo-staff-01',actor_name:'이지은',action_type:'review_changed',previous_status:old,new_status:value,created_at:now()});const saved=persist();render();if(saved)toast(`직원 확인 상태를 ‘${reviewLabels[value]}’로 변경했어요.`);}
// Shared event-shaped adapter: replace browser persistence with the agreed API when a backend is connected.
async function receiveEvent(event){
 if(apiMode){const result=await api('/api/team/call-events',{method:'POST',body:JSON.stringify(event)});await refreshDatabase();return result;}
 if(db.call_events.some(e=>e.source===event.source&&e.event_id===event.event_id))return {applied:false,duplicate:true};
 const p=event.payload||{};let c=callById(event.call_id);
 if(event.event_type==='call_started'){if(c)return {applied:false,conflict:true};c={...p,id:event.call_id,started_at:event.occurred_at,ended_at:null,call_status:'in_progress',review_status:'unreviewed',summary_status:'pending',summary_text:'',data_mode:'simulated',ai_mode:null,summary_source:'fixture',version:1};db.calls.push(c);}
 else if(!c)return {applied:false,missing:true};
 else if(event.event_type==='message_finalized'){if(!db.call_messages.some(m=>m.call_id===c.id&&m.sequence===p.sequence))db.call_messages.push({id:uid(),call_id:c.id,...p,occurred_at:event.occurred_at});}
 else if(event.event_type==='location_observed')db.call_locations.push({id:uid(),call_id:c.id,...p,observed_at:event.occurred_at});
 else if(event.event_type==='call_ended'){c.call_status='ended';c.ended_at=event.occurred_at;}
 else if(event.event_type==='summary_ready'){c.summary_status='ready';c.summary_text=p.summary_text;c.summary_source='fixture';c.ai_mode=null;}
 db.call_events.push({...event,id:uid(),received_at:now()});c.version++;persist();$('#last-update').textContent=`마지막 업데이트 ${time(now())}`;render();return {applied:true};
}
function startSimulation(){
 if(state.playing)return;
 state.playing=true;state.view='dashboard';state.filter='all';state.search='';state.page=0;state.date='';state.detailTab='transcript';$('#search-input').value='';
 const id=`sim-${uid()}`;state.selected=id;let seq=0;
 let eventQueue=Promise.resolve();
 const send=(type,payload)=>{const event={source:'demo',event_id:`${id}-${++seq}`,call_id:id,event_type:type,occurred_at:now(),payload};eventQueue=eventQueue.then(()=>receiveEvent(event)).catch(error=>{timers.forEach(clearTimeout);state.playing=false;render();toast(`시연 저장 실패: ${error.message}`);throw error;});eventQueue.catch(()=>{});return eventQueue;};
 send('call_started',{patient_id:'demo-patient-003',caller_key:'demo-caller-3',name:'이정례',age:82,region:'화순군 화순읍',purpose:'방문 준비 재확인',visit_type:'재진',color:'peach',phone:'010-0000-1003',hospital_code:'KJC_DEMO',planned_visit_text:'다음 방문 전 안내 확인'});
 toast('이정례 님의 가상 통화를 시작했어요. 대화가 순서대로 표시돼요.');
 const steps=[
 [1200,()=>send('location_observed',{latitude:35.057,longitude:126.977,location_source:'simulated',accuracy_m:null})],
 [2200,()=>send('message_finalized',{sequence:1,speaker:'assistant',text:'안녕하세요. 김장철병원 방문 준비 안내입니다. 어떤 내용을 다시 확인하고 싶으세요?'})],
 [4600,()=>send('message_finalized',{sequence:2,speaker:'caller',text:'이정례인데요. 이번에도 혼자 가려고 하는데, 가서 먼저 뭘 해야 하는지 다시 듣고 싶어요.'})],
 [7100,()=>send('message_finalized',{sequence:3,speaker:'assistant',text:'네, 천천히 함께 확인해 볼게요. 병원에서 받으신 방문 안내가 있으신가요?'})],
 [9400,()=>send('message_finalized',{sequence:4,speaker:'caller',text:'네, 받은 안내문이 있어요. 그거 챙겨서 가려고 해요.'})],
 [12000,()=>send('message_finalized',{sequence:5,speaker:'assistant',text:'받으신 안내문을 다시 확인하고 준비해 주세요. 도착하시면 접수처에서 재진 방문이라고 말씀하시고 다음 순서를 확인해 주세요.'})],
 [14600,()=>send('call_ended',{})],
 [16700,()=>{state.playing=false;state.detailTab='summary';send('summary_ready',{summary_text:'이정례 님은 혼자 병원을 방문할 예정이며, 도착 후 먼저 할 일을 재확인했습니다. 병원에서 받은 안내문이 있다고 말했습니다. 안내문을 확인하고, 도착 후 접수처에 재진 방문임을 전달해 다음 절차를 확인하도록 설명한 시연입니다.'});toast('통화가 종료되고 시연용 요약이 준비됐어요. 과거 기록도 비교해 보세요.');}]
 ];timers=steps.map(([ms,fn])=>setTimeout(fn,ms));
}
function exportCSV(){
 const rows=filtered();if(!rows.length){toast('내보낼 통화가 없어요.');return;}
 const quote=v=>`"${String(v??'').replace(/^[=+@-]/,"'$&").replace(/"/g,'""')}"`;
 const csv=[['통화 ID','상담자','나이','연락처','지역','방문 목적','연결 시각(KST)','통화 상태','직원 확인','데이터 구분'],...rows.map(c=>[c.id,c.name,c.age,contactNumber(c),c.region,c.purpose,`${date(c.started_at)} ${time(c.started_at)}`,c.call_status==='in_progress'?'통화 중':c.call_status==='ended'?'통화 종료':'연결 종료',reviewLabels[c.review_status],'가상 데이터'])].map(r=>r.map(quote).join(',')).join('\r\n');
 const url=URL.createObjectURL(new Blob(['\ufeff'+csv],{type:'text/csv;charset=utf-8;'})),a=document.createElement('a');a.href=url;a.download=`김장철병원_통화기록_${TODAY}.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast(`현재 조회 중인 통화 ${rows.length}건을 내보냈어요.`);
}
$$('.nav-item').forEach(b=>b.addEventListener('click',()=>setView(b.dataset.view)));
$('.brand').addEventListener('click',()=>setView('dashboard'));
$('#status-tabs').addEventListener('click',e=>{const b=e.target.closest('[data-filter]');if(b){state.filter=b.dataset.filter;state.page=0;renderTable();}});
$('#search-input').addEventListener('input',e=>{state.search=e.target.value.trim();state.page=0;renderTable();});
$('#date-filter').addEventListener('change',e=>{state.date=e.target.value;state.page=0;renderTable();renderMap();});
$('#all-dates').addEventListener('click',()=>{state.date='';state.page=0;renderTable();renderMap();});
$('#call-table').addEventListener('click',e=>{const r=e.target.closest('[data-call-id]');if(r)selectCall(r.dataset.callId);});
$('#call-table').addEventListener('keydown',e=>{if(['Enter',' '].includes(e.key)){const r=e.target.closest('[data-call-id]');if(r){e.preventDefault();selectCall(r.dataset.callId);}}});
$('#prev-page').addEventListener('click',()=>{state.page--;renderTable();});$('#next-page').addEventListener('click',()=>{state.page++;renderTable();});
$('#detail-panel').addEventListener('click',e=>{const tab=e.target.closest('[data-detail-tab]'),history=e.target.closest('[data-history-id]');if(tab){state.detailTab=tab.dataset.detailTab;renderDetail();}if(history)selectCall(history.dataset.historyId,true);if(e.target.closest('#save-note'))saveNote();if(e.target.closest('#copy-contact'))copyContact();if(e.target.closest('#retry-preparation')){preparationCache.delete(preparationKey(selected()));renderDetail();}const metric=e.target.closest('[data-chart-metric]');if(metric){state.chartMetric=metric.dataset.chartMetric;renderDetail();}});
$('#detail-panel').addEventListener('submit',event=>{if(event.target.id!=='preparation-search')return;event.preventDefault();if(!apiMode){toast('표현을 추가해 다시 찾는 기능은 로컬 DB 연결 버전에서 사용할 수 있어요.');return;}preparationQueries.set(state.selected,$('#preparation-query').value.trim());preparationCache.delete(preparationKey(selected()));renderDetail();});
$('#detail-panel').addEventListener('change',e=>{if(e.target.id==='review-select')changeReview(e.target.value);});
$('#detail-panel').addEventListener('input',e=>{if(e.target.id==='note-input'){drafts.set(state.selected,e.target.value);$('#note-counter').textContent=`${e.target.value.length} / 800`;$('#save-note').disabled=!e.target.value.trim();}});
$('#detail-panel').addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key==='Enter'&&e.target.id==='note-input'){e.preventDefault();saveNote();}if(e.target.getAttribute('role')==='tab'&&['ArrowRight','ArrowLeft'].includes(e.key)){e.preventDefault();const tabs=['preparation','chart','summary','transcript','history'],idx=tabs.indexOf(state.detailTab);state.detailTab=tabs[(idx+(e.key==='ArrowRight'?1:4))%5];renderDetail();$(`#tab-${state.detailTab}`).focus();}});
$('#map-canvas').addEventListener('click',e=>{const marker=e.target.closest('[data-map-id]'),zoom=e.target.closest('[data-zoom]');if(marker)selectCall(marker.dataset.mapId,true);if(zoom){state.zoom=Math.min(2.2,Math.max(1,state.zoom+(zoom.dataset.zoom==='in'?.3:-.3)));renderMap();}});
$('#map-canvas').addEventListener('keydown',e=>{const marker=e.target.closest('[data-map-id]');if(marker&&['Enter',' '].includes(e.key)){e.preventDefault();selectCall(marker.dataset.mapId,true);}});
$('#all-notes-list').addEventListener('click',e=>{const a=e.target.closest('[data-note-call]');if(a)selectCall(a.dataset.noteCall);});
$('#all-notes-list').addEventListener('keydown',e=>{const a=e.target.closest('[data-note-call]');if(a&&['Enter',' '].includes(e.key)){e.preventDefault();selectCall(a.dataset.noteCall);}});
$('#notifications-button').addEventListener('click',()=>{const p=$('#notification-panel');p.hidden=!p.hidden;$('#notifications-button').setAttribute('aria-expanded',String(!p.hidden));});
$('#notification-panel').addEventListener('click',e=>{const b=e.target.closest('[data-notification-id]');if(b){setView('dashboard');selectCall(b.dataset.notificationId,true);$('#notification-panel').hidden=true;$('#notifications-button').setAttribute('aria-expanded','false');}});
document.addEventListener('click',e=>{if(!e.target.closest('.notification-wrap')){$('#notification-panel').hidden=true;$('#notifications-button').setAttribute('aria-expanded','false');}if(innerWidth<=700&&!e.target.closest('.sidebar')&&!e.target.closest('#menu-toggle'))$('#sidebar').classList.remove('open');});
document.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();if(['notes','locations'].includes(state.view))setView('history');$('#search-input').focus();}if(e.key==='Escape'){$('#notification-panel').hidden=true;$('#sidebar').classList.remove('open');$('#notifications-button').setAttribute('aria-expanded','false');}});
$('#menu-toggle').addEventListener('click',()=>$('#sidebar').classList.toggle('open'));
$('#start-demo').addEventListener('click',startSimulation);$('#export-button').addEventListener('click',exportCSV);
const help=$('#help-dialog');['help-button','profile-help'].forEach(id=>$(`#${id}`).addEventListener('click',()=>help.showModal()));['close-help','dismiss-help'].forEach(id=>$(`#${id}`).addEventListener('click',()=>help.close()));
help.addEventListener('click',e=>{if(e.target===help){const r=help.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)help.close();}});
$('#reset-demo').addEventListener('click',()=>{if(apiMode){toast('DB 모드에서는 초기화하지 않습니다. 저장된 기록을 보존합니다.');return;}timers.forEach(clearTimeout);timers=[];db=seed();db.patients=window.HospitalPatientCharts.createProfiles();db.chart_fixture_version=2;for(const c of db.calls){if(chartLinks[c.id])c.patient_id=chartLinks[c.id];}drafts.clear();preparationCache.clear();preparationQueries.clear();state={view:'dashboard',filter:'all',search:'',selected:'demo-call-003',detailTab:'preparation',page:0,date:'',zoom:1,playing:false};$('#search-input').value='';persist();help.close();render();toast('가상 데이터를 처음 상태로 되돌렸어요.');});
// A refreshed page cannot resume a timer-driven demo; close only its interrupted simulated call.
for(const c of db.calls)if(!apiMode&&c.id.startsWith('sim-')&&c.call_status==='in_progress'){c.call_status='failed';c.ended_at=now();c.summary_status='failed';}
async function refreshDatabase(){
 const data=await api('/api/team/bootstrap');db=data;
 if(!selected())state.selected=db.calls[0]?.id;
 render();updateStorageLabels();
}
function updateStorageLabels(){if(!apiMode)return;
 document.querySelector('.sidebar-status').innerHTML='<span class="status-dot"></span>SQLite DB 연결 · 가상 데이터';
 document.querySelector('.demo-badge').innerHTML='<span></span>DB LIVE';
 const footer=document.querySelector('.table-footer>span');if(footer)footer.innerHTML='<span class="status-dot"></span>SQLite 저장 · 가상 데이터 · 5초마다 동기화';
 const reset=$('#reset-demo');reset.disabled=true;reset.textContent='DB 기록 보존 중';
 const help=document.querySelector('.help-note');if(help)help.textContent='가상 환자 정보와 통화·메모·확인 상태가 로컬 SQLite DB에 저장됩니다. 실제 병원 EMR 또는 GPS와 연결되지 않습니다.';
}
window.addEventListener('coordination-select-call',event=>selectCall(event.detail.callId,true));
persist();render();updateStorageLabels();
if(apiMode)setInterval(async()=>{if(document.hidden||state.playing||document.activeElement?.matches('textarea,select,input'))return;try{await refreshDatabase();}catch{document.querySelector('.sidebar-status').textContent='DB 동기화 실패 · 서버 확인 필요';}},5000);
})();
