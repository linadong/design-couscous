const $ = (id) => document.getElementById(id);
const show = (id, visible = true) => $(id).classList.toggle('is-hidden', !visible);
let job = null, sourceUrl = null, timer = null, busy = false, previewReady = false;
function setBusy(value) {
  busy = value;
  ['choose-image', 'image-input', 'build-button', 'layer-prompts', 'background-mode', 'fill-prompt', 'cloud-fill-consent', 'cloud-segmentation-consent'].forEach((id) => $(id).disabled = value);
  $('build-button').textContent = value ? 'Processing draft layers…' : 'Build draft layers';
}
function invalidateReview() {
  previewReady = false; $('review-confirmed').checked = false; $('accept-draft').disabled = true;
  show('download-link', false); show('result-preview-wrap', false);
}
function error(message) {
  show('result-card'); show('error-detail'); show('download-link', false);
  $('result-title').textContent = 'Something needs attention.';
  $('result-copy').textContent = 'No new draft has been approved for download.';
  $('error-detail').textContent = message;
}
function optionsChanged() {
  invalidateReview();
  const flux = $('background-mode').value === 'flux';
  show('fill-prompt-label', flux); show('fill-prompt', flux); show('cloud-fill-consent-label', flux);
  show('cloud-segmentation-consent-label', Boolean($('layer-prompts').value.trim()));
}
$('background-mode').onchange = () => { $('cloud-fill-consent').checked = false; optionsChanged(); };
$('layer-prompts').oninput = () => { $('cloud-segmentation-consent').checked = false; optionsChanged(); };
$('fill-prompt').oninput = optionsChanged;
async function post(path, body) {
  const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const data = await response.json(); if (!response.ok) throw new Error(data.error || 'The local request failed.'); return data;
}
function renderPlan(plan) {
  $('auto-plan').replaceChildren();
  const p = document.createElement('p'); p.className = 'plan-title'; p.textContent = 'Automatic draft plan'; $('auto-plan').append(p);
  const list = document.createElement('ul');
  [...(plan.text || []).map((item) => item.name), ...(plan.objects || []).map((name) => name + ' — possibly grouped objects')].forEach((name) => {
    const li = document.createElement('li'); li.textContent = name; list.append(li);
  });
  $('auto-plan').append(list);
  const note = document.createElement('p'); note.textContent = plan.note; $('auto-plan').append(note);
}
$('choose-image').onclick = () => $('image-input').click();
$('image-input').onchange = async () => {
  const file = $('image-input').files[0]; if (!file || busy) return;
  clearTimeout(timer); job = null; invalidateReview(); show('preview-card', false); show('result-card', false); show('retry-status', false);
  history.replaceState(null, '', location.pathname);
  if (file.size > 20 * 1024 * 1024) { error('Choose an image under 20 MB.'); $('image-input').value = ''; return; }
  setBusy(true); $('file-name').textContent = 'Uploading and analysing ' + file.name + '…';
  try {
    const body = new FormData(); body.append('image', file);
    const response = await fetch('/api/upload', {method: 'POST', body}); const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Image upload failed.');
    job = data;
    if (sourceUrl) URL.revokeObjectURL(sourceUrl); sourceUrl = URL.createObjectURL(file); $('source-preview').src = sourceUrl;
    $('file-name').textContent = job.filename + ' · ' + job.dimensions.width + ' × ' + job.dimensions.height + ' px';
    $('layer-prompts').value = ''; $('cloud-segmentation-consent').checked = false; $('cloud-fill-consent').checked = false;
    renderPlan(job.auto_plan); optionsChanged(); show('preview-card');
  } catch (e) { $('file-name').textContent = 'Upload failed'; error(e.message); }
  finally { $('image-input').value = ''; setBusy(false); }
};
async function renderLayers(data) {
  invalidateReview(); $('layer-stage').replaceChildren(); $('layer-controls').replaceChildren(); $('review-warnings').replaceChildren();
  $('layer-stage').style.aspectRatio = data.dimensions.width + ' / ' + data.dimensions.height;
  $('review-summary').textContent = data.layer_count + ' layers generated. Count alone does not prove that every object has been separated.';
  data.review_warnings.forEach((warning) => { const li = document.createElement('li'); li.textContent = warning; $('review-warnings').append(li); });
  const loads = [];
  data.layers.forEach((layer) => {
    const image = document.createElement('img'); image.alt = layer.name; image.dataset.layer = layer.id;
    image.style.display = layer.visible ? '' : 'none'; $('layer-stage').append(image);
    loads.push(new Promise((resolve, reject) => { image.onload = resolve; image.onerror = () => reject(new Error('A layer preview could not load. Check status again before accepting the draft.')); }));
    image.src = '/api/layer-preview?job=' + encodeURIComponent(data.id) + '&layer=' + layer.id + '&t=' + Date.now();
    const row = document.createElement('div'); row.className = 'layer-row';
    const label = document.createElement('label'); const visible = document.createElement('input'); visible.type = 'checkbox'; visible.checked = layer.visible; visible.dataset.layer = layer.id;
    visible.onchange = () => { image.style.display = visible.checked ? '' : 'none'; };
    label.append(visible, document.createTextNode(' ' + layer.name)); row.append(label);
    const offsetLabel = document.createElement('label'); offsetLabel.textContent = 'Move horizontally to inspect occlusion (preview only)';
    const offset = document.createElement('input'); offset.type = 'range'; offset.min = '-30'; offset.max = '30'; offset.value = '0';
    offset.oninput = () => image.style.transform = 'translateX(' + offset.value + '%)';
    offsetLabel.append(offset); row.append(offsetLabel); $('layer-controls').append(row);
  });
  show('result-preview-wrap'); await Promise.all(loads); previewReady = true;
}
async function poll() {
  if (!job) return; const id = job.id; clearTimeout(timer); show('retry-status', false);
  try {
    const response = await fetch('/api/status?job=' + encodeURIComponent(id)); const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not read status.');
    if (!job || job.id !== id) return;
    $('result-copy').textContent = data.message;
    if (data.status === 'review' || data.status === 'complete') {
      setBusy(false); $('result-title').textContent = 'Inspect your draft layers.';
      await renderLayers(data); return;
    }
    if (data.status === 'failed') { setBusy(false); error(data.error || 'Draft build failed.'); return; }
    if (data.status === 'ready') { setBusy(false); error('This image has no build yet. Upload it again to choose a plan and start.'); return; }
    timer = setTimeout(poll, 1500);
  } catch (e) { setBusy(false); error(e.message); show('retry-status'); }
}
$('retry-status').onclick = () => { if (job) { setBusy(true); show('error-detail', false); poll(); } };
$('build-button').onclick = async () => {
  if (!job || busy) return;
  const required = $('layer-prompts').value.split('\n').map((x) => x.trim()).filter(Boolean);
  if (required.length && !$('cloud-segmentation-consent').checked) { error('Confirm external SAM3 segmentation before extracting required objects.'); return; }
  if ($('background-mode').value === 'flux' && !$('cloud-fill-consent').checked) { error('Confirm external background fill before continuing.'); return; }
  invalidateReview(); show('error-detail', false); show('retry-status', false); show('result-card'); setBusy(true);
  $('result-title').textContent = 'Building a draft for inspection…'; $('result-copy').textContent = 'Processing may take several minutes.';
  try {
    await post('/api/build', {job_id: job.id, required_elements: required, fill_method: $('background-mode').value,
      fill_prompt: $('fill-prompt').value.trim(), cloud_fill_consent: $('cloud-fill-consent').checked,
      cloud_segmentation_consent: $('cloud-segmentation-consent').checked});
    history.replaceState(null, '', '?job=' + encodeURIComponent(job.id));
    poll();
  } catch (e) { setBusy(false); error(e.message); }
};
$('show-all').onclick = () => $('layer-controls').querySelectorAll('input[type=checkbox]').forEach((el) => { el.checked = true; el.onchange(); });
$('background-only').onclick = () => $('layer-controls').querySelectorAll('input[type=checkbox]').forEach((el, index) => { el.checked = index === 0; el.onchange(); });
$('reset-offsets').onclick = () => $('layer-controls').querySelectorAll('input[type=range]').forEach((el) => { el.value = '0'; el.oninput(); });
$('review-confirmed').onchange = () => { $('accept-draft').disabled = !previewReady || !$('review-confirmed').checked; show('download-link', false); };
$('accept-draft').onclick = async () => {
  if (!job || !previewReady || !$('review-confirmed').checked) return;
  const id = job.id; $('accept-draft').disabled = true;
  try {
    await post('/api/confirm-review', {job_id: id, acknowledge_limitations: true});
    if (!job || job.id !== id || !previewReady) return;
    $('download-link').href = '/api/download?job=' + encodeURIComponent(id); show('download-link');
  } catch (e) { error(e.message); $('accept-draft').disabled = false; }
};
optionsChanged();
const resumeJob = new URLSearchParams(location.search).get('job');
if (resumeJob && /^[a-f0-9]{32}$/.test(resumeJob)) {
  job = {id: resumeJob}; show('result-card'); setBusy(true); poll();
}
