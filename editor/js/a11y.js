// Dialogs for the keyboard and screen readers: each says that it is a dialog and
// what it is called; when it opens, the focus goes into it and stays there while
// it is open (Tab and Shift+Tab go round inside it); when it closes, the focus goes
// back to where it was. Their "Close" can be reached and pressed with the keyboard.
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]), select:not([disabled]), '
  + 'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

const shown = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);

// The dialog on top: the last open one in the page (they all share one layer).
function topDialog() {
  const open = [...document.querySelectorAll('.modal-back.show')];
  const back = open[open.length - 1];
  return back ? back.querySelector('.modal') || back : null;
}

export function initDialogs() {
  const before = new Map(); // dialog -> what had the focus before it opened
  let count = 0;
  document.querySelectorAll('.modal-back').forEach(back => {
    const box = back.querySelector('.modal') || back;
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');
    box.tabIndex = -1;
    const title = box.querySelector('h2');
    if (title) {
      title.id ||= `dialog-title-${++count}`;
      box.setAttribute('aria-labelledby', title.id);
    }
    box.querySelectorAll('.close').forEach(close => {
      if (close.matches('button')) return;
      close.setAttribute('role', 'button');
      close.tabIndex = 0;
      close.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); close.click(); }
      });
    });
    new MutationObserver(() => {
      const open = back.classList.contains('show');
      if (open && !before.has(back)) {
        before.set(back, document.activeElement);
        // The dialog itself takes the focus (a reader announces it), unless it put it somewhere itself.
        requestAnimationFrame(() => { if (back.classList.contains('show') && !box.contains(document.activeElement)) box.focus({ preventScroll: true }); });
      } else if (!open && before.has(back)) {
        const el = before.get(back);
        before.delete(back);
        if (!topDialog() && el && el !== document.body && document.contains(el) && shown(el)) el.focus({ preventScroll: true });
      }
    }).observe(back, { attributes: true, attributeFilter: ['class'] });
  });

  document.addEventListener('keydown', e => {
    if (e.key !== 'Tab') return;
    const box = topDialog();
    if (!box) return;
    const items = [...box.querySelectorAll(FOCUSABLE)].filter(shown);
    if (!items.length) { e.preventDefault(); box.focus(); return; }
    const first = items[0], last = items[items.length - 1];
    const at = document.activeElement;
    if (!box.contains(at) || (e.shiftKey && (at === first || at === box)) || (!e.shiftKey && at === last)) {
      e.preventDefault();
      (e.shiftKey ? last : first).focus();
    }
  }, true);
}
