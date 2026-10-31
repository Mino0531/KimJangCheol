import {readFile,mkdir,writeFile} from 'node:fs/promises';
const root=new URL('../',import.meta.url);
const [digestCss,digestJs]=await Promise.all(['src/clinical-digest.css','src/clinical-digest.js'].map(p=>readFile(new URL(p,root),'utf8')));
const digestAssets=`<style>${digestCss}</style><script>${digestJs.replace(/<\/script/gi,'<\\/script')}</script>`;
const [html,css,liveCss,appJs]=await Promise.all(['src/hospital-index.html','src/styles.css','src/hospital-live.css','src/hospital-live.js'].map(p=>readFile(new URL(p,root),'utf8')));
const output=html.replace('</head>',()=>`${digestAssets}</head>`).replace('<!--STYLE-->',()=>`<style>${css}\n${liveCss}</style>`).replace('<!--SCRIPT-->',()=>`<script>${appJs.replace(/<\/script/gi,'<\\/script')}</script>`);
await mkdir(new URL('dist/',root),{recursive:true});
await writeFile(new URL('dist/index.html',root),output);
console.log(`Built dashboard: ${Buffer.byteLength(output)} bytes`);
// The previous feature-rich desk stays isolated as explicitly labelled example data.
// It never loads the server's unauthenticated legacy API or real handoff records.
const [referenceHtml,referenceApp,chartJs,prepJs,chartFixture,prepFixture]=await Promise.all([
  'src/legacy-index.html','src/legacy-app.js','src/patient-charts.js','src/preparation.js',
  'backend/fixtures/chart-display-v1.json','backend/fixtures/rag/preparation-display.json'
].map(p=>readFile(new URL(p,root),'utf8')));
const referenceBanner='<div class="example-banner" role="note">예시 자료 화면입니다. 실제 지자체 인계와 분리되어 있습니다. <a href="/">실제 인계 화면으로 돌아가기</a></div>';
const referenceCss=css+'\n.example-banner{position:relative;z-index:50;padding:12px 20px;background:var(--purple-light);color:var(--purple-dark);font-size:13px;text-align:center}.example-banner a{font-weight:700;text-decoration:underline;margin-left:8px}';
const referenceJs=('window.HospitalPreparationFixtures='+prepFixture+';\nwindow.HospitalCommonChartFixtures='+chartFixture+';\n'+chartJs+'\n'+prepJs+'\n'+referenceApp)
  .replaceAll('kjc-hospital-desk-v1','kjc-hospital-example-v1').replaceAll('가상','예시').replaceAll('시연','예시');
const referenceOutput=referenceHtml.replace('<!--STYLE-->',()=>`<style>${referenceCss}</style>`)
  .replace('<main>',()=>`<main>${referenceBanner}`)
  .replaceAll('가상','예시').replaceAll('시연','예시').replace('DESK DEMO GUIDE','DESK GUIDE').replace('>DEMO<','>예시 자료<')
  .replace('<!--SCRIPT-->',()=>`<script>${referenceJs.replace(/<\/script/gi,'<\\/script')}</script>`);
await writeFile(new URL('dist/hospital-reference.html',root),referenceOutput);
console.log(`Built isolated example dashboard: ${Buffer.byteLength(referenceOutput)} bytes`);
const [municipalHtml,municipalCss,municipalJs]=await Promise.all(['src/municipality/index.html','src/municipality/styles.css','src/municipality/app.js'].map(p=>readFile(new URL(p,root),'utf8')));
const municipalOutput=municipalHtml.replace('</head>',()=>`${digestAssets}</head>`).replace('<!--STYLE-->',()=>`<style>${municipalCss}</style>`).replace('<!--SCRIPT-->',()=>`<script>${municipalJs.replace(/<\/script/gi,'<\\/script')}</script>`);
await writeFile(new URL('dist/municipality.html',root),municipalOutput);
console.log(`Built municipality dashboard: ${Buffer.byteLength(municipalOutput)} bytes`);
