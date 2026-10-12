'use strict';
const $ = id => document.getElementById(id);
const state = {selected: null, jobs: [], signatures: {}, polling: false, authenticated: false};
const statusNames = {queued: 'Queued', running: 'Creating', succeeded: 'Ready', audio_ready: 'Audio ready', failed: 'Needs attention', interrupted: 'Interrupted', waiting_for_assets: 'Waiting for assets', waiting_for_capabilities: 'Waiting for motion support'};
function el(tag, text, className) { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node; }
function link(text, url, download = false) { const a = el('a', text); a.href = url; if (download) a.download = ''; return a; }
function showLogin() { state.authenticated = false; $('login-screen').hidden = false; $('app').hidden = true; }
async function api(path, options = {}) {
  const response = await fetch(path, {credentials: 'same-origin', ...options});
  let data; try { data = await response.json(); } catch { throw new Error('The server returned an unreadable response.'); }
  if (!response.ok) {
    if (response.status === 401 && path !== '/api/login') showLogin();
    const detail = Array.isArray(data.detail) ? data.detail.map(e => e.msg).join('; ') : data.detail;
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return data;
}
function post(path, body = {}) { return api(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}); }
function durationLabel(values) { return values.map(v => `${v}s`).join(' + '); }
function generationLabel(job) { return (job.settings.generation_mode === 'long' ? `${job.settings.durations[0] / 60} min · chapters` : durationLabel(job.settings.durations)) + (job.settings.production_target === 'audio' ? ' · audio first' : '') + (job.settings.llm_configuration?.label ? ` · ${job.settings.llm_configuration.label}` : ''); }
function updateGeneration() {
  const long = $('generation-mode').value === 'long';
  $('short-length-row').hidden = long; $('duration').disabled = long;
  $('long-length-row').hidden = !long; $('long-duration').disabled = !long; $('long-duration').required = long;
  $('long-options').hidden = !long;
  $('subject-graph').disabled = !long;
  $('animation-chapters').disabled = !long;
  $('second-version-row').hidden = long;
  $('second-duration-row').hidden = long || !$('second-version').checked;
  $('second-duration').disabled = long || !$('second-version').checked;
}
function status(node, value) { node.className = `status ${value}`; node.textContent = statusNames[value] || value; }
function updateVoices(voices, selected) {
  $('voice').replaceChildren(...voices.map(v => { const o = el('option', v.label); o.value = v.id; o.dataset.hasTranscript = String(v.has_transcript); return o; }));
  if (selected) $('voice').value = selected;
  updateTranscriptVisibility();
}
function updateTranscriptVisibility() {
  $('reference-transcript-row').hidden = $('tts-engine').value !== 'omnivoice' || $('voice').selectedOptions[0]?.dataset.hasTranscript === 'true';
  $('omni-speed-row').hidden = $('tts-engine').value !== 'omnivoice';
}
async function services() {
  try {
    const data = await api('/api/readiness');
    $('service-summary').textContent = data.ready ? 'Services available' : 'Some services need attention';
    $('services').replaceChildren(...data.services.map(s => {
      const row = el('li'); row.append(el('span', s.label), el('span', s.detail, s.ready ? 'ok' : 'bad')); return row;
    }));
  } catch (error) { $('service-summary').textContent = error.message; }
}
function renderJobs() {
  const signature = JSON.stringify(state.jobs.map(j => [j.id, j.status, j.subject, j.settings, state.selected === j.id]));
  if (signature === state.signatures.jobs) return;
  state.signatures.jobs = signature;
  $('job-count').textContent = `${state.jobs.length} saved ${state.jobs.length === 1 ? 'run' : 'runs'}`;
  $('empty').hidden = state.jobs.length !== 0;
  $('jobs').replaceChildren(...state.jobs.map(j => {
    const button = el('button', undefined, `job-card${state.selected === j.id ? ' selected' : ''}`);
    button.type = 'button'; button.title = j.subject; button.setAttribute('aria-pressed', String(state.selected === j.id));
    const badge = el('span'); status(badge, j.status);
    button.append(badge, el('span', j.subject, 'job-title'), el('span', `${generationLabel(j)} advisory · ${j.settings.orientation}`, 'job-meta'));
    button.addEventListener('click', () => selectJob(j.id)); return button;
  }));
}
async function selectJob(id) {
  if (state.selected !== id) {
    state.selected = id; state.signatures.outputs = null; state.signatures.narrations = null;
    $('outputs').replaceChildren(); $('narrations').replaceChildren(); $('music').replaceChildren(); state.signatures.music = null; $('run-log').textContent = '';
    history.replaceState(null, '', `/#run=${encodeURIComponent(id)}`);
  }
  renderJobs();
  await refreshSelected();
}
function renderOutputs(job) {
  const signature = JSON.stringify([job.id, job.outputs]);
  if (state.signatures.outputs === signature) return;
  state.signatures.outputs = signature;
  $('outputs').replaceChildren(...job.outputs.map(o => {
    const card = el('article', undefined, 'video-card');
    if (o.orientation === 'portrait') card.classList.add('portrait');
    const frame = el('div', undefined, 'media-frame');
    const poster = el('button', undefined, 'poster-button'); poster.type = 'button';
    poster.setAttribute('aria-label', `Play ${o.orientation} video`);
    const posterImage = el('img'); posterImage.src = o.poster_url; posterImage.alt = '';
    poster.append(posterImage, el('span', '▶  Play video', 'poster-action'));
    const video = el('video'); video.controls = true; video.preload = 'none'; video.src = o.video_url; video.poster = o.poster_url; video.playsInline = true; video.hidden = true;
    const playbackError = el('p', 'Playback is unavailable in this browser. You can download the MP4 below.', 'playback-error'); playbackError.hidden = true;
    poster.addEventListener('click', async () => {
      poster.hidden = true; video.hidden = false; video.load();
      try { await video.play(); } catch { playbackError.hidden = false; }
    });
    video.addEventListener('error', () => { playbackError.hidden = false; });
    frame.append(poster, video);
    const info = el('div', undefined, 'video-info');
    info.append(el('h3', o.orientation === 'portrait' ? 'Portrait · 9:16' : 'Landscape · 16:9'),
      el('p', `${o.actual_seconds.toFixed(1)} seconds · ${o.point_count} points${o.visual_metrics ? ` · ${o.visual_metrics.visual_beats} shots · ${o.visual_metrics.distinct_images} images` : ''} · ${o.requested_seconds}s advisory`),
      link('Download MP4 ↓', o.video_url, true), el('p', '✓ Media and sampled layout checks passed', 'checks'));
    card.append(frame, playbackError, info); return card;
  }));
}
function renderNarrations(job) {
  const signature = JSON.stringify([job.id, job.narrations]);
  if (state.signatures.narrations === signature) return;
  state.signatures.narrations = signature;
  $('narrations').replaceChildren(...job.narrations.map(n => {
    const section = el('section', undefined, 'narration');
    const audio = el('audio'); audio.controls = true; audio.preload = 'metadata'; audio.src = n.audio_url;
    const details = el('details'); const points = el('ol', undefined, 'script-points');
    points.append(...n.script.points.map(p => { const li = el('li'); li.append(el('strong', p.heading), el('p', p.narration)); return li; }));
    details.append(el('summary', 'Read the points and narration'), points);
    const downloads = el('p', undefined, 'download'); downloads.append(link('Download WAV', n.audio_url, true), document.createTextNode(' · '), link('Download script', n.script_url, true));
    section.append(el('h3', `Narration · ${n.requested_seconds}s brief`),
      el('p', `${n.actual_seconds.toFixed(1)} seconds · ${n.point_count} points. Ready for listening while the visuals finish.`, 'hint'), audio, details, downloads);
    return section;
  }));
}
async function refreshSelected() {
  if (!state.selected) { $('run').hidden = true; return; }
  const selected = state.selected;
  const job = await api(`/api/jobs/${encodeURIComponent(selected)}`);
  if (selected !== state.selected) return;
  $('run').hidden = false;
  $('run-title').textContent = job.subject;
  const mood = job.creative_brief?.resolved_mood || job.settings.mood || 'auto';
  $('run-meta').textContent = `${generationLabel(job)} advisory · ${job.settings.orientation} · ${job.settings.tts_engine === 'omnivoice' ? 'OmniVoice' : 'VibeVoice'} · ${mood} · ${job.settings.music_engine === 'acestep' ? 'ACE-Step score' : 'no score'}`;
  if (job.settings.generation_mode === 'long') $('run-meta').textContent += ` · subject graph ${job.settings.subject_graph ? 'on' : 'off'}`;
  if (job.settings.animation_chapters) $('run-meta').textContent += ' · animation chapters enabled';
  $('run-meta').textContent += ` · image review ${job.settings.image_review === false ? 'off' : 'on'}${job.settings.premium_rescue ? ' · premium rescue' : ''}`;
  const resumeSettings = JSON.stringify([job.id, job.settings.image_review]);
  if (state.signatures.resumeSettings !== resumeSettings) {
    $('resume-image-review').value = job.settings.image_review === false ? 'off' : 'on';
    state.signatures.resumeSettings = resumeSettings;
  }
  status($('run-status'), job.status);
  $('stages').replaceChildren(...job.stages.map((s, i) => {
    const li = el('li', undefined, s.state); li.append(el('span', s.state === 'succeeded' ? '✓' : s.state === 'skipped' ? '–' : String(i + 1), 'stage-icon'), el('span', s.label)); return li;
  }));
  const event = job.latest_event;
  const beatStatus = beat => beat && beat.beat_id ? ` · beat ${beat.position}/${beat.total} · ${beat.beat_id}${beat.phase ? ' · ' + beat.phase : ''}` : '';
  $('run-event').textContent = job.status === 'succeeded' ? 'Your film is ready. Watch it here or open the full review page.'
    : job.status === 'queued' ? 'Waiting for the current run to finish.'
    : event ? `${event.stage.replace(/^pipeline:/, '').replaceAll('-', ' ')} · ${event.state}${event.detail.state ? ' · ' + event.detail.state : ''}${beatStatus(event.detail.beat_progress || event.detail)}${event.detail.elapsed_seconds !== undefined ? ' · ' + event.detail.elapsed_seconds + 's elapsed' : ''}`
    : 'Starting the saved pipeline…';
  const paused = ['waiting_for_assets', 'waiting_for_capabilities'].includes(job.status);
  $('run-error').hidden = !['failed', 'interrupted', 'waiting_for_assets', 'waiting_for_capabilities'].includes(job.status);
  $('run-error-text').textContent = job.error || '';
  $('resume').textContent = paused ? 'Resume after cache fill' : 'Resume saved run';
  $('resume').disabled = job.status === 'waiting_for_assets' && !job.can_resume_assets;
  $('asset-waits').replaceChildren(...(job.asset_waits || []).map(wait => {
    const section = el('section');
    section.append(el('h3', `${wait.chapter} · ${wait.kind === 'assets' ? (wait.ready ? 'Assets ready' : 'Drawing assets needed') : 'Motion support needed'}`));
    const list = el('ul');
    for (const request of wait.unresolved || wait.requests) list.append(el('li', request.description || request.reason || request.requested_asset_id || request.asset_id || JSON.stringify(request)));
    section.append(list); return section;
  }));
  $('chapter-plan-link').replaceChildren();
  if (job.chapter_plan_url) $('chapter-plan-link').append(link('Download chapter plan', job.chapter_plan_url, true));
  $('chapter-progress').textContent = job.chapter_progress ? `${job.chapter_progress.completed} / ${job.chapter_progress.total} chapters ready${job.chapter_progress.failed ? ` · ${job.chapter_progress.failed} need attention` : ''}` : '';
  const chapterSignature = JSON.stringify([job.id, job.chapters]);
  if (state.signatures.chapters !== chapterSignature) {
    state.signatures.chapters = chapterSignature;
    $('chapters').replaceChildren(...(job.chapters || []).map(c => {
      const card = el('article', undefined, 'chapter-card');
      const badge = el('span'); status(badge, c.status);
      const scopeLabel = c.scope.start_year == null && c.scope.end_year == null ? 'Undated focus' : c.scope.start_year == null ? `Through ${c.scope.end_year}` : c.scope.end_year == null ? `From ${c.scope.start_year}` : `${c.scope.start_year}–${c.scope.end_year}`;
      card.append(badge, el('h3', `${c.id} · ${c.title}`), el('p', `${scopeLabel} · ${c.advisory_seconds}s advisory · ${c.mood}`, 'hint'));
      if (c.stage) card.append(el('p', `Current step: ${c.stage}`, 'hint'));
      if (c.beat_progress) card.append(el('p', `${c.beat_progress.orientation || ''}${beatStatus(c.beat_progress)}`, 'hint'));
      if (c.production_kind === 'animation') card.append(el('p', 'Animation chapter · Luna', 'hint'));
      if (c.error) card.append(el('p', c.error, 'error'));
      if (c.audio_url) { const audio = el('audio'); audio.controls = true; audio.preload = 'none'; audio.src = c.audio_url; card.append(audio); }
      const downloads = el('p');
      if (c.script_url) downloads.append(link('Read script', c.script_url));
      if (c.animation_report_url) { downloads.append(document.createTextNode(' · ')); const a = link('Animation plan & preview', c.animation_report_url); a.target = '_blank'; a.rel = 'noopener'; downloads.append(a); }
      if (c.asset_requests_url) { downloads.append(document.createTextNode(' · '), link('Download asset demands', c.asset_requests_url, true)); }
      if (c.review_url) { downloads.append(document.createTextNode(' · ')); const a = link('Watch chapter', c.review_url); a.target = '_blank'; a.rel = 'noopener'; downloads.append(a); }
      card.append(downloads); return card;
    }));
  }
  $('revise-music').hidden = !job.can_revise_music;
  $('revise-narration').hidden = !job.can_revise_narration;
  $('continue-video').hidden = !job.can_continue_video;
  const audioSignature = JSON.stringify([job.id, job.audio_deliveries]);
  if (state.signatures.audioDeliveries !== audioSignature) {
    state.signatures.audioDeliveries = audioSignature;
    $('audio-deliveries').replaceChildren(...(job.audio_deliveries || []).map(item => {
      const section = el('section', undefined, 'narration');
      const audio = el('audio'); audio.controls = true; audio.preload = 'metadata'; audio.src = item.mp3_url;
      const downloads = el('p'); downloads.append(link('Download MP3', item.mp3_url, true), document.createTextNode(' · '),
        link('Download WAV', item.master_url, true), document.createTextNode(' · '), link('Read full narration', item.script_url));
      section.append(el('h3', 'Complete audio for review'), el('p', `${item.actual_seconds.toFixed(1)} seconds · ${item.point_count} points`, 'hint'), audio, downloads);
      return section;
    }));
  }
  $('run-log').textContent = job.log || job.events.map(e => `[${new Date(e.time * 1000).toLocaleTimeString()}] ${e.stage}: ${e.state}`).join('\n');
  renderOutputs(job); renderNarrations(job);
  const musicSignature = JSON.stringify([job.id, job.music]);
  if (state.signatures.music !== musicSignature) {
    state.signatures.music = musicSignature;
    $('music').replaceChildren(...(job.music || []).map(m => {
      const section = el('section', undefined, 'narration');
      const audio = el('audio'); audio.controls = true; audio.preload = 'metadata'; audio.src = m.master_url;
      const downloads = el('p', undefined, 'download'); downloads.append(link('Download score', m.score_url, true), document.createTextNode(' · '), link('Download mixed WAV', m.master_url, true));
      section.append(el('h3', `Narration with score · ${m.requested_seconds}s brief`), audio, downloads); return section;
    }));
  }
  if ($('review-link').dataset.url !== (job.review_url || '')) {
    $('review-link').dataset.url = job.review_url || ''; $('review-link').replaceChildren();
    if (job.review_url) { const a = link('Open full review & sources ↗', job.review_url); a.className = 'review-button'; a.target = '_blank'; a.rel = 'noopener'; $('review-link').append(a); }
  }
}
async function refresh() {
  if (!state.authenticated || state.polling) return;
  state.polling = true;
  try {
    const data = await api('/api/jobs'); state.jobs = data.jobs;
    if (!state.selected && state.jobs.length) state.selected = state.jobs[0].id;
    renderJobs(); await refreshSelected(); $('connection').textContent = 'Saved on the server';
  } catch (error) { $('connection').textContent = error.message; }
  finally { state.polling = false; }
}
async function enter() {
  state.authenticated = true; $('login-screen').hidden = true; $('app').hidden = false;
  const settings = await api('/api/settings'); updateVoices(settings.voices);
  for (const [id, key] of [['mood','moods'], ['mood-intensity','mood_intensities'], ['visual-balance','visual_balances'], ['music-engine','music_engines']]) {
    $(id).replaceChildren(...settings[key].map(value => { const option = el('option', value.label); option.value = value.id; return option; }));
  }
  $('mood').value = settings.default_mood; $('omni-speed').value = settings.default_omni_speed;
  const hash = new URLSearchParams(location.hash.slice(1)); state.selected = hash.get('run');
  await Promise.all([services(), refresh(), loadLLM()]);
}
// ---------------------------------------------------------------- LLM settings
function option(value, label) { const node = el('option', label); node.value = value; return node; }
function connectionName(id) { const c = state.llm?.connections[id]; return c ? `${c.label} · ${c.model || 'server model'}` : id; }
async function loadLLM() { try { renderLLM(await api('/api/llm-settings')); } catch (error) { $('llm-roles-error').textContent = error.message; } }
function renderLLM(data) {
  state.llm = data;
  const ids = Object.keys(data.connections);
  $('llm-roles').replaceChildren(...data.roles.map(role => {
    const box = el('div', undefined, 'llm-role'); const label = el('label', role.label); label.htmlFor = `llm-role-${role.id}`;
    const select = el('select'); select.id = `llm-role-${role.id}`;
    const usable = ids.filter(id => !role.types || role.types.includes(data.connections[id].type));
    select.replaceChildren(...(role.optional ? [option('', 'None')] : []),
      ...usable.map(id => option(id, connectionName(id) + (role.needs_images && !data.connections[id].vision ? ' (no images)' : ''))));
    select.value = data.assignments[role.id] || ''; box.append(label, select, el('p', role.description, 'hint')); return box;
  }));
  const configs = Object.entries(data.configurations);
  $('llm-configs').replaceChildren(...(configs.length ? configs.map(([id, c]) => {
    const row = el('div', undefined, 'llm-item'); const text = el('div');
    text.append(el('strong', c.label + (data.active_configuration === id ? ' · active' : '')),
                el('small', `Planner: ${connectionName(c.roles.planner)} · Reviewer: ${connectionName(c.roles.reviewer)}` +
                            (c.roles.premium_image ? ` · Premium images: ${connectionName(c.roles.premium_image)}` : '')));
    if (c.note) text.append(el('small', c.note));
    const actions = el('div', undefined, 'llm-actions');
    const apply = el('button', 'Apply', 'secondary'); apply.type = 'button'; apply.onclick = () => llmAction('/api/llm-settings/configurations/apply', {id}, 'llm-config-error');
    const remove = el('button', 'Delete', 'quiet'); remove.type = 'button'; remove.onclick = () => confirm(`Delete configuration "${c.label}"?`) && llmAction('/api/llm-settings/configurations/delete', {id}, 'llm-config-error');
    actions.append(apply, remove); row.append(text, actions); return row;
  }) : [el('p', 'No saved configurations yet.', 'hint')]));
  $('llm-connections').replaceChildren(...ids.map(id => {
    const c = data.connections[id], row = el('div', undefined, 'llm-item'), text = el('div');
    const key = c.key_set === null ? 'no key needed' : c.key_set ? 'key set' : 'KEY MISSING';
    text.append(el('strong', c.label), el('small', `${data.types[c.type].label} · ${c.url} · ${c.model || 'server model'}`),
                el('small', `${c.vision ? 'images' : 'text only'} · ${c.max_concurrency || 1} at once · ${key}` + (c.in_use.length ? ` · in use: ${c.in_use.join(', ')}` : '')));
    const actions = el('div', undefined, 'llm-actions');
    const edit = el('button', 'Edit', 'secondary'); edit.type = 'button'; edit.onclick = () => editConnection(id);
    const remove = el('button', 'Delete', 'quiet'); remove.type = 'button'; remove.onclick = () => confirm(`Delete connection "${c.label}"?`) && llmAction('/api/llm-settings/connections/delete', {id}, 'llm-form-error');
    actions.append(edit, remove); row.append(text, actions); return row;
  }));
  if (!$('llm-type').options.length) $('llm-type').replaceChildren(...Object.entries(data.types).map(([id, t]) => option(id, t.label)));
  const chosen = $('llm-configuration').value;
  $('llm-configuration').replaceChildren(option('', `Current LLM settings (${connectionName(data.assignments.planner)} → ${connectionName(data.assignments.reviewer)})`),
    ...configs.map(([id, c]) => option(id, c.label)));
  $('llm-configuration').value = configs.some(([id]) => id === chosen) ? chosen : '';
  if (!$('llm-id').value) resetConnectionForm();
}
async function llmAction(path, body, errorId) {
  $(errorId).textContent = '';
  try { renderLLM(await post(path, body)); return true; } catch (error) { $(errorId).textContent = error.message; return false; }
}
function updateConnectionType() {
  const type = state.llm?.types[$('llm-type').value]; if (!type) return;
  $('llm-key-row').hidden = !type.needs_key; $('llm-effort-row').hidden = $('llm-type').value !== 'openai_responses';
  if (!$('llm-id').value) { $('llm-url').value = type.default_url; $('llm-concurrency').value = type.default_concurrency; }
  $('llm-key-hint').textContent = type.needs_key ? `Saved keys stay on the server and are never shown again. Without one, ${type.key_env} from the service environment is used.` : '';
}
function resetConnectionForm() {
  $('llm-id').value = ''; $('llm-form').reset(); $('llm-form-title').textContent = 'Add a connection'; $('llm-cancel').hidden = true;
  $('llm-type').disabled = false; $('llm-timeout').value = 900; updateConnectionType();
}
function editConnection(id) {
  const c = state.llm.connections[id]; $('llm-id').value = id; $('llm-form-title').textContent = `Edit “${c.label}”`; $('llm-cancel').hidden = false;
  $('llm-type').value = c.type; $('llm-type').disabled = true; $('llm-label').value = c.label; $('llm-url').value = c.url; $('llm-model').value = c.model || '';
  $('llm-concurrency').value = c.max_concurrency || 1; $('llm-timeout').value = c.timeout || 900; $('llm-vision').checked = !!c.vision;
  $('llm-effort').value = c.reasoning_effort || 'low'; $('llm-key').value = ''; updateConnectionType();
  $('llm-form').scrollIntoView({behavior: 'smooth', block: 'start'});
}
$('llm-open').addEventListener('click', () => { $('llm-view').hidden = false; document.querySelector('main.workspace').hidden = true; loadLLM(); });
$('llm-close').addEventListener('click', () => { $('llm-view').hidden = true; document.querySelector('main.workspace').hidden = false; });
$('llm-type').addEventListener('change', updateConnectionType);
$('llm-cancel').addEventListener('click', resetConnectionForm);
function selectedRoles() { return Object.fromEntries(state.llm.roles.map(role => [role.id, $(`llm-role-${role.id}`).value])); }
$('llm-save-roles').addEventListener('click', () => llmAction('/api/llm-settings/roles', selectedRoles(), 'llm-roles-error'));
$('llm-save-config').addEventListener('click', async () => {
  const roles = selectedRoles();   // what is selected now, even if "Save roles" was not clicked
  if (await llmAction('/api/llm-settings/configurations', {label: $('llm-config-name').value, note: $('llm-config-note').value, roles}, 'llm-config-error')) { $('llm-config-name').value = ''; $('llm-config-note').value = ''; }
});
$('llm-detect').addEventListener('click', async event => {
  event.target.disabled = true; $('llm-form-error').textContent = '';
  try {
    const data = await post('/api/llm-settings/detect', {type: $('llm-type').value, url: $('llm-url').value, id: $('llm-id').value || null, api_key: $('llm-key').value || null});
    $('llm-model-list').replaceChildren(...data.models.map(id => option(id, id)));
    $('llm-form-error').textContent = `${data.models.length} models listed; start typing in Model to pick one.`;
  } catch (error) { $('llm-form-error').textContent = error.message; }
  finally { event.target.disabled = false; }
});
$('llm-form').addEventListener('submit', async event => {
  event.preventDefault();
  const body = {id: $('llm-id').value || null, type: $('llm-type').value, label: $('llm-label').value, url: $('llm-url').value,
    model: $('llm-model').value, vision: $('llm-vision').checked, timeout: Number($('llm-timeout').value) || 900,
    max_concurrency: Number($('llm-concurrency').value) || null, reasoning_effort: $('llm-effort').value, api_key: $('llm-key').value || null};
  if (await llmAction('/api/llm-settings/connections', body, 'llm-form-error')) resetConnectionForm();
});
$('login-form').addEventListener('submit', async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true; $('login-error').textContent = '';
  const password = $('password').value; $('password').value = '';
  try { await post('/api/login', {username: $('username').value, password}); await enter(); }
  catch (error) { $('login-error').textContent = error.message; }
  finally { button.disabled = false; }
});
$('logout').addEventListener('click', async () => { try { await post('/api/logout'); } finally { showLogin(); $('outputs').replaceChildren(); $('narrations').replaceChildren(); $('music').replaceChildren(); state.signatures = {}; } });
$('second-version').addEventListener('change', updateGeneration);
$('generation-mode').addEventListener('change', updateGeneration);
updateGeneration();
$('tts-engine').addEventListener('change', updateTranscriptVisibility);
$('voice').addEventListener('change', updateTranscriptVisibility);
document.querySelectorAll('[data-subject]').forEach(button => button.addEventListener('click', () => { $('subject').value = button.dataset.subject; $('subject').focus(); }));
$('create-form').addEventListener('submit', async event => {
  event.preventDefault(); $('create-button').disabled = true; $('create-error').textContent = '';
  const long = $('generation-mode').value === 'long';
  const durations = [long ? Number($('long-duration').value) * 60 : Number($('duration').value)]; if (!long && $('second-version').checked) durations.push(Number($('second-duration').value));
  try {
    const job = await post('/api/jobs', {subject: $('subject').value, durations, orientation: $('orientation').value,
      review_mode: $('review-mode').checked ? 'experimental' : 'off', voice_id: $('voice').value,
      image_review: $('image-review').value === 'on', premium_rescue: $('premium-rescue').checked,
      tts_engine: $('tts-engine').value, reference_transcript: $('reference-transcript').value,
      mood: $('mood').value, mood_intensity: $('mood-intensity').value, visual_balance: $('visual-balance').value,
      music_engine: $('music-engine').value, omni_speed: Number($('omni-speed').value),
      generation_mode: $('generation-mode').value,
      production_target: $('production-target').value,
      subject_graph: long && $('subject-graph').value === 'on',
      animation_chapters: long && $('animation-chapters').value === 'on',
      llm_configuration: $('llm-configuration').value || null,
      submission_id: (globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`)});
    state.jobs.unshift(job); await selectJob(job.id); await refresh();
    if (window.innerWidth < 820) $('run').scrollIntoView({behavior: 'smooth', block: 'start'});
  } catch (error) { $('create-error').textContent = error.message; }
  finally { $('create-button').disabled = false; }
});
$('voice-file').addEventListener('change', async event => {
  const file = event.target.files[0]; if (!file) return;
  $('voice-status').textContent = 'Uploading…'; $('create-button').disabled = true;
  const form = new FormData(); form.append('file', file);
  try { const voice = await api('/api/voices', {method: 'POST', body: form}); const settings = await api('/api/settings'); updateVoices(settings.voices, voice.id); $('voice-status').textContent = 'Added'; }
  catch (error) { $('voice-status').textContent = error.message; }
  finally { $('create-button').disabled = false; event.target.value = ''; }
});
$('resume').addEventListener('click', async event => {
  event.target.disabled = true;
  try { await post(`/api/jobs/${encodeURIComponent(state.selected)}/resume`, {image_review: $('resume-image-review').value === 'on'}); await refresh(); }
  catch (error) { $('run-error-text').textContent = error.message; }
  finally { event.target.disabled = false; }
});
$('continue-video-button').addEventListener('click', async event => {
  event.target.disabled = true; $('continue-video-error').textContent = '';
  try { await post(`/api/jobs/${encodeURIComponent(state.selected)}/continue-video`); await refresh(); }
  catch (error) { $('continue-video-error').textContent = error.message; }
  finally { event.target.disabled = false; }
});
$('revise-music-button').addEventListener('click', async event => {
  event.target.disabled = true; $('revise-music-error').textContent = '';
  try {
    await post(`/api/jobs/${encodeURIComponent(state.selected)}/music`, {music_engine: $('revision-music-engine').value});
    await refresh();
  } catch (error) { $('revise-music-error').textContent = error.message; }
  finally { event.target.disabled = false; }
});
$('revise-narration-button').addEventListener('click', async event => {
  event.target.disabled = true; $('revise-narration-error').textContent = '';
  try {
    await post(`/api/jobs/${encodeURIComponent(state.selected)}/narration`);
    await refresh();
  } catch (error) { $('revise-narration-error').textContent = error.message; }
  finally { event.target.disabled = false; }
});
$('refresh-services').addEventListener('click', event => { event.preventDefault(); services(); });
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
setInterval(() => { if (!document.hidden) refresh(); }, 5000);
api('/api/session').then(session => session.authenticated ? enter() : showLogin()).catch(error => { $('login-error').textContent = error.message; });
