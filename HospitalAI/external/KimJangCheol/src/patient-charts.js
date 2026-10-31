/* Synthetic, hand-authored chart fixtures. No clinical system or model supplies these records. */
(()=>{
'use strict';
const escape=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pillIcon='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="m9 4-5 5a6 6 0 0 0 8 9l7-7a6 6 0 0 0-9-8L9 4Zm-2 3 10 10"/></svg>';
const conditionData=[
 [['고혈압','2018년 기록'],['골다공증','2022년 기록']],
 [['만성 폐쇄성 폐질환','2020년 기록'],['고혈압','2016년 기록']],
 [['고혈압','2015년 기록'],['제2형 당뇨병','2019년 기록'],['무릎 골관절염','2021년 기록']],
 [['이상지질혈증','2020년 기록'],['갑상선기능저하증','2018년 기록']],
 [['고혈압','2021년 기록']],
 [['제2형 당뇨병','2017년 기록'],['이상지질혈증','2020년 기록']],
 [['무릎 골관절염','2018년 기록'],['골다공증','2021년 기록']],
 [],
 [['고혈압','2019년 기록'],['이상지질혈증','2022년 기록']],
 [['만성 위염','2023년 기록']],
 [['제2형 당뇨병','2021년 기록']],
 [['고혈압','2012년 기록']]
];
const medicationFor={
 '고혈압':['혈압관리정 A','혈압 관리'],
 '제2형 당뇨병':['혈당관리정 B','혈당 관리'],
 '무릎 골관절염':['관절관리정 C','관절 증상 관리'],
 '골다공증':['골건강관리정 D','골 건강 관리'],
 '이상지질혈증':['지질관리정 E','지질 관리'],
 '갑상선기능저하증':['갑상선관리정 F','갑상선 관리'],
 '만성 폐쇄성 폐질환':['호흡관리흡입제 G','호흡기 관리'],
 '만성 위염':['위장관리정 H','위장 증상 관리']
};
function createProfiles(){if(Array.isArray(window.HospitalCommonChartFixtures))return JSON.parse(JSON.stringify(window.HospitalCommonChartFixtures));return conditionData.map((conditions,index)=>{
 const n=String(index+1).padStart(3,'0'),noData=index===7;
 const meds=conditions.map(([condition],m)=>({id:`demo-med-${n}-${m+1}`,name:medicationFor[condition][0],purpose:medicationFor[condition][1],started_on:m?'2026-09-11':'2026-08-14',status:'복용 중',source:'가상 수기 기록',is_fictional:true}));
 const dates=['2026-07-17','2026-08-14','2026-09-11','2026-10-23'];
 return {
  id:`demo-patient-${n}`,chart_number:`KJC-2026-${n}`,source:'가상 수기 기록',as_of:'2026-10-23',sex:[0,2,3,4,6,8,10].includes(index)?'여':'남',
  conditions:conditions.map(([name,since])=>({name,since,source:'시연용 등록 정보'})),
  allergies:index===0?{status:'기록 있음',text:'페니실린계 · 가상 알레르기 기록'}:{status:'미확인',text:'알레르기 확인 기록이 없습니다.'},
  current_medications:meds,
  prescriptions:noData?[]:[{id:`demo-rx-${n}-2`,date:'2026-10-23',department:conditions.some(c=>c[0].includes('골관절'))?'내과·정형외과':'내과',provider:'기존 의료기관 · 가상',medications:meds.map(m=>m.name),status:'최근 처방 기록'},{id:`demo-rx-${n}-1`,date:'2026-09-11',department:'내과',provider:'기존 의료기관 · 가상',medications:meds.slice(0,Math.max(1,meds.length-1)).map(m=>m.name),status:'이전 처방 기록'}],
  observations:noData?[]:dates.map((at,j)=>({date:at,systolic:[146,141,138,134][j]+index%4,diastolic:[87,85,83,81][j]+index%3,glucose:[153,146,139,131][j]+index*2})),
  encounters:noData?[]:[{date:'2026-10-23',department:'외래',title:'만성질환 정기 확인',note:'지병과 복용약 목록을 확인한 가상 기록입니다.'},{date:'2026-09-11',department:'외래',title:'경과 확인 및 처방 기록',note:'기존 의료기관에서 받은 가상 진료 기록입니다.'},{date:'2026-08-14',department:'외래',title:'정기 상담',note:'시연을 위해 작성한 방문 이력입니다.'}],
  is_fictional:true
 };
});}
const shortDate=s=>s.slice(5).replace('-','/');
const chartTitle=(title,count)=>`<div class="chart-section-title"><h4>${title}</h4>${count===undefined?'':`<span>${count}개</span>`}</div>`;
function trend(patient,metric){
 const pressure=metric!=='glucose',rows=(patient.observations||[]).filter(r=>pressure?Number.isFinite(r.systolic)&&Number.isFinite(r.diastolic):Number.isFinite(r.glucose)).sort((a,b)=>a.date.localeCompare(b.date));
 const controls=`<div class="trend-switch" role="group" aria-label="추이 지표"><button data-chart-metric="blood_pressure" aria-pressed="${pressure}" class="${pressure?'selected':''}">혈압</button><button data-chart-metric="glucose" aria-pressed="${!pressure}" class="${!pressure?'selected':''}">혈당</button></div>`;
 if(!rows.length)return `<section class="chart-section"><div class="chart-section-title"><h4>최근 측정 추이</h4>${controls}</div><p class="chart-empty">등록된 측정 기록이 없습니다.</p></section>`;
 const keys=pressure?['systolic','diastolic']:['glucose'],values=rows.flatMap(r=>keys.map(k=>r[k])),min=Math.floor((Math.min(...values)-12)/10)*10,max=Math.ceil((Math.max(...values)+12)/10)*10;
 const x=i=>37+i*246/(Math.max(rows.length-1,1)),y=v=>107-(v-min)/(max-min)*77;
 const series=keys.map((key,index)=>{
  const color=index?'#8fc1b4':'#9a7bd3',label=pressure?(index?'이완기':'수축기'):'혈당';
  return `<polyline points="${rows.map((r,i)=>`${x(i)},${y(r[key])}`).join(' ')}" fill="none" stroke="${color}" stroke-width="2.3" stroke-linejoin="round"/>${rows.map((r,i)=>`<circle cx="${x(i)}" cy="${y(r[key])}" r="3.5" fill="white" stroke="${color}" stroke-width="1.8"><title>${r.date} · ${label} ${r[key]} ${pressure?'mmHg':'mg/dL'} · 가상 측정값</title></circle>`).join('')}`;
 }).join('');
 const last=rows.at(-1);
 return `<section class="chart-section"><div class="chart-section-title"><h4>최근 측정 추이</h4>${controls}</div><div class="trend-card"><div class="trend-latest"><span>최근 기록 <small>${shortDate(last.date)}</small></span><strong>${pressure?`${last.systolic}<em>/</em>${last.diastolic}`:last.glucose}<small>${pressure?'mmHg':'mg/dL'}</small></strong></div><svg viewBox="0 0 320 140" class="vital-chart" role="img" aria-label="${pressure?'혈압':'혈당'} 가상 측정 추이: ${rows.map(r=>`${r.date} ${pressure?`${r.systolic}/${r.diastolic}`:r.glucose}`).join(', ')}">${[0,1,2].map(i=>{const v=min+i*(max-min)/2;return `<line x1="35" x2="290" y1="${y(v)}" y2="${y(v)}" stroke="#eeeaf4" stroke-dasharray="3 4"/><text x="25" y="${y(v)+3}" text-anchor="end" fill="#b4acbf" font-size="8">${v}</text>`;}).join('')}${series}${rows.map((r,i)=>`<text x="${x(i)}" y="129" text-anchor="middle" fill="#b0a9bb" font-size="8">${shortDate(r.date)}</text>`).join('')}</svg><div class="trend-legend">${pressure?'<span><i></i>수축기</span><span><i class="mint-line"></i>이완기</span>':'<span><i></i>혈당</span>'}<small>가상 측정값 · 임상 판단 없음</small></div></div></section>`;
}
function preparation(patient){
 const p=patient.visit_preparation;if(!p)return '';
 const labels={mobility:{independent:'혼자 이동 가능',needs_support:'이동 도움 필요',wheelchair:'휠체어 이용',unknown:'미확인'},hearing:{standard:'일반 안내',needs_slow_speech:'천천히 설명 필요',unknown:'미확인'},companion:{planned:'동행 예정',not_planned:'동행 계획 없음',unknown:'미확인'},phone_type:{feature_phone:'폴더폰',smartphone:'스마트폰',unknown:'미확인'}};
 return `<section class="chart-section preparation-section">${chartTitle('방문 준비 참고')}<div class="preparation-grid">${[['mobility','이동'],['hearing','음성 안내'],['companion','동행'],['phone_type','휴대전화']].map(([key,title])=>`<div><small>${title}</small><strong>${escape(labels[key][p[key]]||'미확인')}</strong></div>`).join('')}</div>${p.staff_note?`<p class="preparation-note">${escape(p.staff_note)}</p>`:''}</section>`;
}
function render(patient,metric='blood_pressure'){
 if(!patient)return '<div class="chart-unlinked"><strong>연결된 환자 차트가 없어요.</strong><p>이 통화에는 환자 기록이 지정되어 있지 않습니다. 연락처만으로 의료정보를 연결하지 않습니다.</p></div>';
 return `<div class="patient-chart" data-patient-id="${escape(patient.id)}"><div class="chart-source"><span class="chart-source-dot"></span><strong>가상 환자 차트</strong><span>${patient.schema_version?`공통 양식 v${escape(patient.schema_version)}`:'EMR 미연동'}</span></div><div class="chart-patient-meta"><span>${escape(patient.chart_number)} · ${escape(patient.sex)}</span><span>자료 기준 ${patient.as_of.replaceAll('-','.')}</span></div>${preparation(patient)}<section class="chart-section">${chartTitle('지병 · 기저질환',patient.conditions.length)}<div class="condition-chips">${patient.conditions.map(c=>`<div class="condition-chip"><strong>${escape(c.name)}</strong><small>${escape(c.since)}${c.status==='resolved'?' · 과거 기록':''}</small></div>`).join('')||`<p class="chart-empty">${patient.knowledge_status?.conditions==='none_reported'?'지병이 없다고 보고된 기록입니다.':'등록된 지병 정보가 없습니다. 건강 상태가 확인됐다는 의미는 아닙니다.'}</p>`}</div><div class="allergy-line"><span class="allergy-symbol">!</span><div><strong>알레르기 <small>${escape(patient.allergies.status)}</small></strong><p>${escape(patient.allergies.text)}</p></div></div></section><section class="chart-section">${chartTitle('현재 복용 중인 약',patient.current_medications.length)}<div class="medication-list">${patient.current_medications.map(m=>`<article class="medication-row"><span class="medication-icon">${pillIcon}</span><div><strong>${escape(m.name)}<span class="fictional-drug">가상약품</span></strong><p>${escape(m.purpose)} · ${escape(m.source)}</p></div><span class="medication-status">${escape(m.status)}</span></article>`).join('')||'<p class="chart-empty">확인된 복용약 기록이 없습니다.</p>'}</div><p class="chart-disclaimer">약품명은 시연을 위해 만든 이름입니다. 실제 처방·복용 안내가 아닙니다.</p></section>${trend(patient,metric)}<section class="chart-section">${chartTitle('이전 처방 내역',patient.prescriptions.length)}<div class="prescription-list">${patient.prescriptions.map((p,i)=>`<details class="prescription-record" ${i===0?'open':''}><summary><span><strong>${p.date.replaceAll('-','.')}</strong><small>${escape(p.department)} · ${p.medications.length}종</small></span><span class="prescription-label">${escape(p.status)} <b>⌄</b></span></summary><div class="prescription-content"><p>${escape(p.provider)}</p><ul>${p.medications.map(n=>`<li>${escape(n)} <small>가상약품</small></li>`).join('')}</ul><small>보관된 처방 이력 · 현재 복용 여부와 별도</small></div></details>`).join('')||'<p class="chart-empty">등록된 처방 이력이 없습니다.</p>'}</div></section><section class="chart-section encounters-section">${chartTitle('최근 진료 기록')}<div class="encounter-list">${patient.encounters.map(e=>`<article class="encounter-item"><i></i><div><header><strong>${escape(e.title)}</strong><time>${shortDate(e.date)}</time></header><p>${escape(e.note)}</p></div></article>`).join('')||'<p class="chart-empty">등록된 진료 기록이 없습니다.</p>'}</div></section><div class="chart-footer-note">시연용으로 수기 작성한 자료이며, 통화 내용에서 AI가 추정한 의료정보가 아닙니다.</div></div>`;
}
window.HospitalPatientCharts=Object.freeze({createProfiles,render});
})();
