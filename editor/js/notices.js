// Messages: every toast and every alert is kept, so one that vanished too fast can
// be read again under the bell in the top bar. The bell counts what needs attention
// (alerts); other messages only light a dot.
import { escapeHtml } from './util.js';

export const notices = []; // newest first
const unread = { alerts: 0, other: 0 };

// From the editor (initNotices); messages logged before only wait for it.
let t = s => s, lang = () => 'en', ready = false;

export function logNotice(msg, kind = 'info') {
  notices.unshift({ msg: String(msg), kind, at: new Date() });
  notices.length = Math.min(notices.length, 100);
  unread[kind === 'alert' ? 'alerts' : 'other']++;
  if (ready) renderNotices();
}

export function renderNotices() {
  const badge = document.getElementById('notes-badge');
  badge.hidden = !unread.alerts && !unread.other;
  badge.classList.toggle('dot', !unread.alerts);
  badge.textContent = unread.alerts ? (unread.alerts > 9 ? '9+' : String(unread.alerts)) : '';
  const list = document.getElementById('notes-list');
  list.innerHTML = notices.length ? '' : `<div class="notes-empty">${escapeHtml(t('No messages yet.'))}</div>`;
  const locale = lang() === 'de' ? 'de-DE' : 'en-GB';
  notices.forEach(m => {
    const row = document.createElement('div');
    row.className = `note ${m.kind}`;
    row.innerHTML = `<span class="note-time">${m.at.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' })}</span><span class="note-msg">${escapeHtml(t(m.msg))}</span>`;
    list.appendChild(row);
  });
}

export function initNotices(ctx) {
  ({ t, lang } = ctx);
  ready = true;
  const read = () => { unread.alerts = 0; unread.other = 0; };
  document.getElementById('bnotes').addEventListener('click', () => { read(); renderNotices(); });
  document.getElementById('notes-clear').onclick = () => { notices.length = 0; read(); renderNotices(); };
  renderNotices();
}
