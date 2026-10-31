import {readFile, writeFile, mkdir} from 'node:fs/promises';
const here = new URL('../', import.meta.url);
const shared = new URL('../../../external/KimJangCheol/src/', import.meta.url);
const hospitalCss = await readFile(new URL('styles.css', shared), 'utf8') + '\n' + await readFile(new URL('hospital-live.css', shared), 'utf8');
const municipalCss = await readFile(new URL('municipality/styles.css', shared), 'utf8');
for (const [mode, outputDir] of [['manual-', 'dist'], ['', 'dist-web-login']]) {
  await mkdir(new URL(`${outputDir}/`, here), {recursive:true});
  for (const [name, css, output] of [['hospital', hospitalCss, 'index.html'], ['municipality', municipalCss, 'municipality.html']]) {
    const html = await readFile(new URL(`src/${mode}${name}.html`, here), 'utf8');
    const js = await readFile(new URL(`src/${mode}${name}.js`, here), 'utf8');
    if (!html.includes('<!--STYLE-->') || !html.includes('<!--SCRIPT-->')) throw new Error(`Missing template marker: ${mode}${name}`);
    const page = html.replace('<!--STYLE-->', () => `<style>${css}</style>`).replace('<!--SCRIPT-->', () => `<script>${js.replace(/<\/script/gi, '<\\/script')}</script>`);
    await writeFile(new URL(`${outputDir}/${output}`, here), page.replace(/\r\n/g, '\n'));
    console.log(`Built ${outputDir}/${output}: ${Buffer.byteLength(page)} bytes`);
  }
}
