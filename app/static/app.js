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
// A page swap would wipe an open chat, a pending answer or an unsent draft.
const chatBusy = () => document.querySelector('.chat[open], .chat-typing') || [...document.querySelectorAll('.chat-form textarea')].some(field => field.value.trim());
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
      if (state.status !== 'running' && state.archive_status !== 'running' && !chatBusy()) {
        const page = await fetch(location.href, {cache: 'no-store'});
        if (!page.ok) throw new Error('Report unavailable');
        const html = new DOMParser().parseFromString(await page.text(), 'text/html');
        const main = html.querySelector('#main');
        if (!main) throw new Error('Report unavailable');
        if (!chatBusy()) {
          document.querySelector('#main').replaceWith(main);
          initializePage();
          return;
        }
      }
    } catch {
      const progress = document.querySelector('#run-progress');
      if (progress) progress.textContent = 'Verbindung unterbrochen – erneuter Versuch folgt …';
    }
    setTimeout(poll, 3000);
  };
  setTimeout(poll, 1000);
}
for (const form of document.querySelectorAll('form:not(.chat-form)')) {
  form.addEventListener('submit', () => {
    const button = form.querySelector('button');
    if (button) { button.disabled = true; button.textContent = 'Wird verarbeitet …'; }
  });
}
for (const chat of document.querySelectorAll('details.chat')) {
  const log = chat.querySelector('.chat-log');
  const form = chat.querySelector('.chat-form');
  const field = form.querySelector('textarea');
  const button = form.querySelector('button');
  const scrollTo = (element = log.lastElementChild) => { if (element) log.scrollTop = element.offsetTop; };
  const grow = () => { field.style.height = 'auto'; field.style.height = `${field.scrollHeight}px`; };
  const add = (className, text) => {
    const element = document.createElement('div');
    element.className = className;
    if (text) element.textContent = text;
    log.append(element);
    return element;
  };
  chat.addEventListener('toggle', () => { if (chat.open) scrollTo(); });
  field.addEventListener('input', grow);
  field.addEventListener('keydown', event => {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing || event.keyCode === 229 || matchMedia('(pointer: coarse)').matches) return;
    event.preventDefault();
    if (field.value.trim()) form.requestSubmit();
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const question = field.value.trim();
    if (button.disabled || !question) return;
    const body = new URLSearchParams(new FormData(form));
    log.querySelector('.chat-empty')?.remove();
    log.querySelector('.chat-error')?.remove();
    const bubble = add('chat-user', question);
    const typing = add('chat-typing');
    typing.append(document.createElement('i'), document.createElement('i'), document.createElement('i'), document.createElement('span'));
    typing.lastChild.textContent = form.dataset.pending;
    field.value = '';
    grow();
    button.disabled = true;
    scrollTo(bubble);
    let result = {};
    let ok = false;
    try {
      const response = await fetch(form.action, {method: 'POST', body});
      result = await response.json().catch(() => ({}));
      ok = response.ok && typeof result.answer_html === 'string';
    } catch {}
    typing.remove();
    if (ok) {
      bubble.textContent = result.question;
      const answer = add('chat-assistant');
      answer.innerHTML = result.answer_html;
      log.querySelector('.chat-note')?.remove();
      form.dataset.pending = 'Antwort wird erstellt …';
      scrollTo(answer);
    } else {
      bubble.remove();
      field.value = field.value.trim() ? `${question}\n\n${field.value}` : question;
      grow();
      scrollTo(add('notice error chat-error', result.error || 'Keine Antwort erhalten. Verbindung prüfen; eine inzwischen fertige Antwort erscheint nach dem Neuladen der Seite.'));
    }
    button.disabled = false;
    field.focus();
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
