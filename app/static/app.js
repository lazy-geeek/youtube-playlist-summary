function initializePage() {
const count = document.querySelector('#count');
if (count) {
  fetch('/api/count').then(async response => {
    const result = await response.json();
    const detail = document.querySelector('#count-detail');
    if (result.error) { detail.textContent = result.error; return; }
    count.textContent = result.pending;
    detail.textContent = `${result.total} in der Quelle${result.held ? ` · ${result.held} bereits in offenen Berichten` : ''}`;
    const button = document.querySelector('#start-button');
    button.disabled = button.dataset.ready !== 'yes' || result.pending === 0;
  }).catch(() => { document.querySelector('#count-detail').textContent = 'Playlist derzeit nicht erreichbar.'; });
}
const runState = document.querySelector('#run-state');
if (runState?.dataset.running === 'yes') {
  const poll = async () => {
    if (!runState.isConnected) return;
    try {
      const response = await fetch(`/api/runs/${runState.dataset.runId}/status`, {cache: 'no-store'});
      if (!response.ok) throw new Error('Status unavailable');
      const state = await response.json();
      for (const [name, value] of Object.entries(state.counts)) {
        const counter = document.querySelector(`[data-count="${name}"]`);
        if (counter) counter.textContent = value;
      }
      const progress = document.querySelector('#run-progress');
      const total = Object.values(state.counts).reduce((sum, value) => sum + value, 0);
      if (progress) progress.textContent = state.status === 'running'
        ? `${total - state.counts.pending} von ${total} Videos verarbeitet`
        : 'Briefing erstellt. Videos werden archiviert …';
      if (state.status !== 'running' && state.archive_status !== 'running') {
        const page = await fetch(location.href, {cache: 'no-store'});
        if (!page.ok) throw new Error('Report unavailable');
        const html = new DOMParser().parseFromString(await page.text(), 'text/html');
        const main = html.querySelector('#main');
        if (!main) throw new Error('Report unavailable');
        document.querySelector('#main').replaceWith(main);
        initializePage();
        return;
      }
    } catch {
      const progress = document.querySelector('#run-progress');
      if (progress) progress.textContent = 'Verbindung unterbrochen – erneuter Versuch folgt …';
    }
    setTimeout(poll, 3000);
  };
  setTimeout(poll, 1000);
}
for (const form of document.querySelectorAll('form')) {
  form.addEventListener('submit', () => {
    const button = form.querySelector('button');
    if (button) { button.disabled = true; button.textContent = 'Wird verarbeitet …'; }
  });
}
// Chapter navigation opens a completed section without changing its read state.
for (const link of document.querySelectorAll('[data-read-chapter]')) {
  link.addEventListener('click', () => {
    const chapter = document.querySelector(`#chapter-${link.dataset.readChapter}`);
    if (chapter) {
      chapter.open = true;
      const group = chapter.closest('.read-sections');
      if (group) group.open = true;
    }
  });
}
const dateFormat = new Intl.DateTimeFormat('de-DE', {
  day: '2-digit', month: 'short', year: 'numeric',
  hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Berlin'
});
for (const time of document.querySelectorAll('time[datetime]')) {
  const date = new Date(time.dateTime);
  if (!Number.isNaN(date.getTime())) time.textContent = dateFormat.format(date);
}

}
initializePage();
