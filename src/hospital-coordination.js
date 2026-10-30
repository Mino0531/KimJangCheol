(() => {
'use strict';
const apiMode = document.querySelector('meta[name="hospital-data-source"]')?.content === 'api';
const KEY = 'kjc-municipality-v1', panel = document.querySelector('#coordination-inbox');
const esc = v => String(v ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let referrals = [], selected = null, busy = false;
const link = document.querySelector('#municipality-link'); if(link)link.href=apiMode?'/municipality':'municipality.html';
const dialog = document.createElement('dialog');dialog.id='hospital-response-dialog';
dialog.innerHTML='<div class="dialog-header"><h2>지자체 연계 요청에 답변</h2><button class="icon-button" id="close-hospital-response" aria-label="답변 창 닫기">✕</button></div><div id="referral-request"></div><form id="hospital-response-form"><label for="hospital-response-text">방문 지원 담당자에게 보낼 답변</label><textarea id="hospital-response-text" rows="5" maxlength="1500" required placeholder="접수 창구, 안내 확인 방법 등 요청에 대한 답변을 적어 주세요."></textarea><p class="hospital-response-note">이 앱의 지자체 화면으로 전달됩니다. 실제 외부 병원·지자체 시스템과 연결되지 않습니다.</p><div role="status" id="hospital-response-status"></div><button type="submit" class="button primary full">답변 저장·지자체에 전달</button></form>';
document.body.append(dialog);
async function request(path,options={}){const r=await fetch(path,{...options,headers:{'Content-Type':'application/json'}});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(typeof d.detail==='string'?d.detail:'요청에 실패했습니다. 입력값을 확인해 주세요.');return d;}
function readOffline(){let data;try{data=JSON.parse(localStorage.getItem(KEY));}catch{}return data?.fixture_version===1?data:structuredClone(window.MunicipalityFixtures);}
async function load(){
 if(!panel)return;
 try{referrals=apiMode?(await request('/api/team/hospital/referrals')).referrals:readOffline().cases.filter(c=>c.hospital_request);const pending=referrals.filter(c=>c.status==='hospital_requested');
 panel.innerHTML=`<div class="hospital-inbox-heading"><div><h2>지자체 연계 요청 <span class="count-badge">${pending.length}</span></h2><p>방문 지원 담당자가 어르신을 대신해 확인을 요청했어요.</p></div><a href="${apiMode?'/municipality':'municipality.html'}" target="_blank" rel="noopener">지자체 대시보드 ↗</a></div><div class="hospital-referral-list">${pending.slice(0,4).map(c=>`<div class="hospital-referral"><button data-referral-call="${esc(c.call_id)}"><strong>${esc(c.call.name)} <small>${esc(c.call.region)}</small></strong><span>${esc(c.hospital_request)}</span></button><button class="button quiet" data-response="${esc(c.id)}">답변 작성</button></div>`).join('')||'<div class="hospital-inbox-empty">현재 답변을 기다리는 요청이 없어요.</div>'}</div>${pending.length>4?`<p class="hospital-more">대기 ${pending.length}건 · 지자체 화면에서 전체 업무를 확인하세요.</p>`:''}`;
 }catch{panel.innerHTML='<div class="hospital-inbox-empty">지자체 연계 요청을 불러오지 못했어요. 서버 연결을 확인해 주세요.</div>';}
}
if(panel)panel.addEventListener('click',e=>{const call=e.target.closest('[data-referral-call]');if(call)window.dispatchEvent(new CustomEvent('coordination-select-call',{detail:{callId:call.dataset.referralCall}}));const b=e.target.closest('[data-response]');if(!b)return;selected=referrals.find(c=>c.id===b.dataset.response);if(!selected)return;document.querySelector('#referral-request').innerHTML=`<strong>${esc(selected.call.name)} · ${esc(selected.call.region)}</strong><p>${esc(selected.hospital_request)}</p><small>담당자 ${esc(selected.assignee)} · 김장철병원 연계</small>`;document.querySelector('#hospital-response-text').value='';document.querySelector('#hospital-response-status').textContent='';dialog.showModal();});
document.querySelector('#close-hospital-response').addEventListener('click',()=>{if(!busy)dialog.close();});
document.querySelector('#hospital-response-form').addEventListener('submit',async e=>{e.preventDefault();if(busy||!selected)return;const text=document.querySelector('#hospital-response-text').value.trim();if(!text)return;busy=true;const b=e.target.querySelector('button[type=submit]');b.disabled=true;
 try{if(apiMode)await request(`/api/team/hospital/referrals/${encodeURIComponent(selected.id)}/responses`,{method:'POST',body:JSON.stringify({expected_version:selected.version,response_text:text})});else{const data=readOffline(),c=data.cases.find(x=>x.id===selected.id);if(!c||c.version!==selected.version||c.status!=='hospital_requested')throw new Error('변경된 요청입니다. 창을 닫고 최신 목록에서 다시 선택해 주세요.');const at=new Date().toISOString();Object.assign(c,{status:'response_received',version:c.version+1,hospital_response:text,hospital_responded_at:at,updated_at:at});data.actions.push({id:globalThis.crypto?.randomUUID?.()||String(Date.now()),case_id:c.id,action_type:'hospital_responded',actor_name:'이지은',actor_role:'병원',note:text,created_at:at});localStorage.setItem(KEY,JSON.stringify(data));}
 dialog.close();await load();
 }catch(error){document.querySelector('#hospital-response-status').textContent=error.message;}finally{busy=false;b.disabled=false;}
});
load();setInterval(()=>{if(!document.hidden&&!dialog.open&&!busy)load();},8000);
})();
