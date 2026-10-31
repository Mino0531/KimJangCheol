(() => {
  const cache = new Map();
  const element = (tag, text, className = '') => {
    const node = document.createElement(tag); node.className = className;
    if (text) node.textContent = text;
    return node;
  };
  async function load(options) {
    const path = options.scope === 'hospital'
      ? `/api/team/municipality-handoffs/${encodeURIComponent(options.callId)}/summary`
      : `/api/municipality/inbox/${encodeURIComponent(options.callId)}/summary`;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(path, {cache:'no-store', signal:controller.signal});
      if (!response.ok) throw new Error('summary_unavailable');
      return await response.json();
    } finally { clearTimeout(timer); }
  }
  function render(container, data, retry) {
    container.replaceChildren(element('h3', 'AI 상담 정리'));
    if (data.status !== 'ready') {
      container.append(element('p', 'AI 정리를 완료하지 못했습니다. 아래 상담 원문을 확인해 주세요.', 'digest-note'));
      const button = element('button', '다시 정리하기', 'button secondary'); button.type = 'button'; button.addEventListener('click', retry); container.append(button);
      return;
    }
    container.append(element('h4', '아픈 부위 · 증상'));
    const symptoms = Array.isArray(data.symptoms) ? data.symptoms : [];
    if (!symptoms.length) container.append(element('p', '주민이 직접 말한 증상이 아직 확인되지 않았습니다.'));
    for (const symptom of symptoms) {
      const item = element('div', '', 'digest-item');
      item.append(element('strong', symptom.body_part), element('p', symptom.description));
      const evidence = element('details'); evidence.append(element('summary', '주민이 말한 원문'), element('blockquote', symptom.quote));
      item.append(evidence); container.append(item);
    }
    if (Array.isArray(data.facts) && data.facts.length) {
      container.append(element('h4', '발생 시점 · 함께 확인할 내용'));
      for (const fact of data.facts) {
        const item = element('div', '', 'digest-item'); item.append(element('p', fact.description));
        const evidence = element('details'); evidence.append(element('summary', '주민이 말한 원문'), element('blockquote', fact.quote));
        item.append(evidence); container.append(item);
      }
    }
    container.append(element('p', data.notice, 'digest-note'));
  }
  function mount(container, options) {
    container.className = 'clinical-digest'; container.setAttribute('aria-label', 'AI 상담 정리');
    container.setAttribute('aria-live', 'polite');
    const key = `${options.scope}:${options.callId}:${options.version || ''}`;
    container.dataset.summaryKey = key;
    container.replaceChildren(element('h3', 'AI 상담 정리'), element('p', '주민이 말한 아픈 부위와 증상을 정리하고 있습니다.', 'digest-note'));
    if (!cache.has(key)) {
      if (cache.size >= 128) cache.delete(cache.keys().next().value);
      cache.set(key, load(options).catch(() => ({status:'unavailable'})));
    }
    void cache.get(key).then(data => {
      if (!container.isConnected || container.dataset.summaryKey !== key) return;
      render(container, data, () => { cache.delete(key); mount(container, options); });
    });
  }
  window.ConsultationDigest = {mount};
  document.addEventListener('DOMContentLoaded', () => {
    const brand = document.querySelector('#sidebar .brand');
    if (brand) { const home = element('a', '서비스 메인으로', 'digest-home'); home.href = '/'; brand.after(home); }
  });
})();
