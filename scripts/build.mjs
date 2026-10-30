import {readFile,mkdir,writeFile} from 'node:fs/promises';
const root=new URL('../',import.meta.url);
const load=p=>readFile(new URL(p,root),'utf8');
const [html,css,chartJs,prepJs,appJs,coordJs,muniHtml,muniCss,muniJs]=await Promise.all(['src/index.html','src/styles.css','src/patient-charts.js','src/preparation.js','src/app.js','src/hospital-coordination.js','src/municipality/index.html','src/municipality/styles.css','src/municipality/app.js'].map(load));
const [fixture,preparations,demo,coordination]=await Promise.all(['backend/fixtures/chart-display-v1.json','backend/fixtures/rag/preparation-display.json','backend/fixtures/demo.json','backend/fixtures/coordination.json'].map(load));
const database=JSON.parse(demo),coord=JSON.parse(coordination);
const [safetyJs,safetyGuidance,safetyScenarios]=await Promise.all(['src/municipality/safety.js','backend/fixtures/safety-guidance.json','backend/fixtures/safety-scenarios.json'].map(load));
const scenarios=JSON.parse(safetyScenarios);
for(const c of coord.cases)c.safety=scenarios[c.id]||null;
const callKeys=['id','name','age','phone','region','purpose','visit_type','started_at','ended_at','call_status','planned_visit_text','summary_status','summary_text'];
const calls=database.calls.map(c=>Object.fromEntries(callKeys.map(k=>[k,c[k]])));
const muniFixture={fixture_version:1,storage:'browser',data_mode:'simulated',municipality:'화순군',safety_guidance:JSON.parse(safetyGuidance),assignees:['미배정','김하늘','박서윤','정다은'],categories:['방문 절차','이동 지원 확인','준비사항 재확인','연락 지원'],calls,cases:coord.cases.map(c=>({...c,call:calls.find(x=>x.id===c.call_id)})),actions:coord.actions,messages:database.call_messages,locations:database.call_locations};
const shared='window.MunicipalityFixtures='+JSON.stringify(muniFixture)+';\n';
const hospitalJs=shared+'window.HospitalPreparationFixtures='+preparations+';\n'+'window.HospitalCommonChartFixtures='+fixture+';\n'+chartJs+'\n'+prepJs+'\n'+appJs+'\n'+coordJs;
function bundle(template,style,script){return template.replace('<!--STYLE-->',()=>`<style>${style}</style>`).replace('<!--SCRIPT-->',()=>`<script>${script.replace(/<\/script/gi,'<\\/script')}</script>`);}
await mkdir(new URL('dist/',root),{recursive:true});
for(const [name,template,style,script]of [['index.html',html,css,hospitalJs],['municipality.html',muniHtml,muniCss,shared+safetyJs+'\n'+muniJs]]){
 const output=bundle(template,style,script);await writeFile(new URL('dist/'+name,root),output);console.log(`Built ${name}: ${Buffer.byteLength(output)} bytes`);
}
