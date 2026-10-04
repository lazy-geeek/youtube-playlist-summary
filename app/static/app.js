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
if (document.querySelector('#run-state')?.dataset.running === 'yes') {
  setTimeout(() => location.reload(), 6000);
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
