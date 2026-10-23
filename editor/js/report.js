// "Create a bug report": a zip to attach to an issue. The app adds versions and its
// log (without sign-in, keys or anything that looks like a token), the page its last
// messages and errors and a few counts - no names of files, no photos, no projects.

// From the editor (initReport).
let t, closeMenus, openModal, saveTextFile, pageInfo;
export function initReport(ctx) {
  ({ t, closeMenus, openModal, saveTextFile, pageInfo } = ctx);
}

const ISSUES_URL = 'https://github.com/F0uP/streetview-tour-uploader/issues/new';

export function openReport() {
  closeMenus();
  document.getElementById('report-status').textContent = '';
  openModal('report-back');
}

document.getElementById('report-save').onclick = async () => {
  const status = document.getElementById('report-status');
  status.textContent = t('Putting the report together…');
  try {
    const r = await fetch('/api/report', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(pageInfo()) });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const stamp = new Date().toISOString().slice(0, 16).replace(/[-:]/g, '').replace('T', '-');
    await saveTextFile(`streetview-tour-editor-report-${stamp}.zip`, await r.blob());
    status.textContent = t('Saved. Attach the zip file to your report on GitHub and describe what you did.');
  } catch (err) {
    status.textContent = t('The report could not be made: {msg}', { msg: err.message || err });
  }
};
document.getElementById('report-issue').onclick = () => window.open(ISSUES_URL, '_blank', 'noopener');
