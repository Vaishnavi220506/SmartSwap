const $ = (selector, root = document) => root.querySelector(selector);
const esc = (value) => String(value ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmt = (value, digits = 1) => (value == null || Number.isNaN(value) ? '–' : Number(value).toFixed(digits));
const pct = (value) => (value == null ? '–' : `${(value * 100).toFixed(0)}%`);
const pval = (value) => (value == null ? '–' : value < 0.001 ? '<0.001' : value.toFixed(3));
const getJSON = async (url, options) => { const r = await fetch(url, options); const body = await r.json().catch(() => ({})); if (!r.ok) throw new Error(body.detail || r.statusText); return body; };
const post = (url, body) => getJSON(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body || {}) });
const toast = (message) => { const node = $('#toast'); node.textContent = message; node.style.display = 'block'; clearTimeout(toast.t); toast.t = setTimeout(() => { node.style.display = 'none'; }, 3200); };

const NAMES = {
  idle: 'No interference', cpu: 'CPU', membw: 'Memory bandwidth', io: 'Storage reads', mixed: 'Mixed',
  none: 'No mitigation', observe: 'Observe only', nice: 'Static priority', static: 'SmartSwap v1 (static cap)', adaptive: 'SmartSwap v2 (adaptive)',
  adaptive_noladder: 'v2 without ladder', adaptive_noaimd: 'v2 without AIMD', adaptive_nobenefit: 'v2 without benefit check',
};
const PRESET_INFO = {
  cpu: 'Twice as many busy workers as CPU cores', membw: 'Workers streaming large buffers through memory', io: '16 workers issuing 1 MiB unbuffered reads',
  mixed: 'CPU, memory and storage load together', idle: 'Control: no background work at all',
};
const POLICY_INFO = {
  adaptive: 'Measures stalls, names the cause, applies the cheapest fix, verifies it helped', nice: 'Always lower background CPU and I/O priority',
  static: 'Always cap background CPU at 35%, boost foreground', none: 'Do nothing (the reference)', observe: 'Run the canary and controller, but never act',
};
const CAUSE = {
  cpu: ['CPU', 'The canary waits to be scheduled: busy cores are delaying it.'],
  mem: ['Memory bandwidth', 'Its memory copy is slow while scheduling is fine: something is saturating memory.'],
  io: ['Storage', 'Its disk read is slow while compute and memory are fine: the storage queue is busy.'],
  none: ['Nothing notable', 'No component of the canary is slowed beyond twice its idle time.'],
};

/* ---------- navigation ---------- */
const pages = ['monitor', 'experiment', 'evidence', 'reports'];
let currentPage = 'monitor';
function navigate(page) {
  if (!pages.includes(page)) page = 'monitor';
  currentPage = page;
  document.querySelectorAll('.page').forEach((node) => node.classList.toggle('active', node.id === `page-${page}`));
  document.querySelectorAll('nav a').forEach((node) => { const on = node.dataset.page === page; node.classList.toggle('active', on); node.toggleAttribute('aria-current', on); });
  if (page === 'experiment') loadRuns();
  if (page === 'evidence') { loadStudyProgress(); loadResearch(); }
  if (page === 'monitor') refreshProcesses();
  if (page === 'reports') loadCaps();
  history.replaceState(null, '', `#${page}`);
  window.scrollTo(0, 0);
}
document.querySelectorAll('nav a').forEach((a) => a.addEventListener('click', (event) => { event.preventDefault(); navigate(a.dataset.page); }));
window.addEventListener('hashchange', () => navigate(location.hash.slice(1)));

/* ---------- SVG line chart with crosshair tooltip ---------- */
function lineChart(el, { xs, series, yMax, yLabel = '', thresholds = [], shade = [], xFormat = (x) => x, height }) {
  const W = el.clientWidth || 600; const H = height || el.clientHeight || 220;
  const m = { l: 40, r: 12, t: 10, b: 24 }; const iw = W - m.l - m.r; const ih = H - m.t - m.b;
  if (!xs.length) { el.innerHTML = '<p class="muted small">Waiting for data…</p>'; return; }
  const x0 = xs[0]; const x1 = xs[xs.length - 1] === x0 ? x0 + 1 : xs[xs.length - 1];
  const top = yMax || Math.max(1, ...series.flatMap((s) => s.values.filter((v) => v != null))) * 1.1;
  const X = (x) => m.l + ((x - x0) / (x1 - x0)) * iw; const Y = (y) => m.t + ih - (Math.min(y, top) / top) * ih;
  const ticks = [0, top / 2, top].map((v) => `<line class="gridline" x1="${m.l}" x2="${W - m.r}" y1="${Y(v)}" y2="${Y(v)}"/><text class="axis" x="${m.l - 6}" y="${Y(v) + 4}" text-anchor="end">${v >= 10 ? v.toFixed(0) : v.toFixed(1)}</text>`).join('');
  const shades = shade.map(([a, b]) => `<rect class="engaged" x="${X(a)}" y="${m.t}" width="${Math.max(1, X(b) - X(a))}" height="${ih}"/>`).join('');
  const lines = thresholds.map((t) => `<line class="threshold" stroke-dasharray="${t.dash || '4 3'}" x1="${m.l}" x2="${W - m.r}" y1="${Y(t.y)}" y2="${Y(t.y)}"/>`).join('');
  const paths = series.map((s) => {
    const d = s.values.map((v, i) => (v == null ? null : `${X(xs[i]).toFixed(1)},${Y(v).toFixed(1)}`)).filter(Boolean).join(' L');
    return `<path class="line" style="stroke:${s.color}" d="M${d}"/>`;
  }).join('');
  const xt = `<text class="axis" x="${m.l}" y="${H - 6}">${esc(xFormat(x0))}</text><text class="axis" x="${W - m.r}" y="${H - 6}" text-anchor="end">${esc(xFormat(x1))}</text>`;
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${shades}${ticks}${lines}${paths}${xt}<text class="axis" x="${m.l}" y="${m.t - 0}" dy="-2">${esc(yLabel)}</text><line class="cross" y1="${m.t}" y2="${m.t + ih}" visibility="hidden"/><rect x="${m.l}" y="${m.t}" width="${iw}" height="${ih}" fill="transparent"/></svg>`;
  const svg = $('svg', el); const cross = $('.cross', svg); const tip = $('#tooltip');
  svg.addEventListener('pointermove', (event) => {
    const box = svg.getBoundingClientRect(); const px = ((event.clientX - box.left) / box.width) * W;
    let i = 0; let best = Infinity; xs.forEach((x, k) => { const d = Math.abs(X(x) - px); if (d < best) { best = d; i = k; } });
    cross.setAttribute('x1', X(xs[i])); cross.setAttribute('x2', X(xs[i])); cross.setAttribute('visibility', 'visible');
    tip.innerHTML = `<div class="muted">${esc(xFormat(xs[i]))}</div>` + series.map((s) => `<div><span style="color:${s.color}">■</span> ${esc(s.name)}: <b>${s.values[i] == null ? '–' : fmt(s.values[i], 2)}${esc(s.unit || '')}</b></div>`).join('') + (series[0].extra ? `<div class="muted">${esc(series[0].extra(i))}</div>` : '');
    tip.hidden = false; tip.style.left = `${Math.min(window.innerWidth - 270, event.clientX + 14)}px`; tip.style.top = `${event.clientY + 14}px`;
  });
  svg.addEventListener('pointerleave', () => { cross.setAttribute('visibility', 'hidden'); tip.hidden = true; });
}
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/* ---------- host + system (always live, no canary needed) ---------- */
const sysHistory = [];  // {t, cpu, mem}
function sparkline(el, values, color) {
  const W = 200; const H = 46; const top = Math.max(10, ...values) * 1.1;
  const pts = values.map((v, i) => `${((i / Math.max(1, values.length - 1)) * W).toFixed(1)},${(H - (v / top) * (H - 4) - 2).toFixed(1)}`).join(' ');
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><polyline points="0,${H} ${pts} ${W},${H}" style="fill:${color};opacity:.12;stroke:none"/><polyline points="${pts}" style="fill:none;stroke:${color};stroke-width:2;vector-effect:non-scaling-stroke"/></svg>`;
}
function setMeter(id, value) {
  const bar = $(`#${id}`); bar.style.width = `${Math.min(100, value)}%`;
  bar.parentElement.className = `meter ${value >= 90 ? 'crit' : value >= 75 ? 'warn' : ''}`;
}
async function refreshSystem() {
  let m;
  try { m = await getJSON('/api/metrics'); } catch { $('#host-status').className = 'host bad'; $('#host-status span').textContent = 'Service unreachable'; return; }
  $('#host-status').className = 'host ok'; $('#host-status span').textContent = 'Native Windows service online';
  sysHistory.push({ t: Date.now() / 1000, cpu: m.cpu_percent, mem: m.memory_load }); if (sysHistory.length > 120) sysHistory.shift();
  $('#cpu-value').textContent = fmt(m.cpu_percent, 0);
  $('#mem-value').textContent = fmt(m.memory_load, 0); setMeter('mem-meter', m.memory_load);
  $('#mem-detail').textContent = `${(m.available_mb / 1024).toFixed(1)} GB free of ${(m.total_physical_mb / 1024).toFixed(0)} GB`;
  const commitPct = (m.commit_used_mb / m.commit_limit_mb) * 100;
  $('#commit-value').textContent = (m.commit_used_mb / 1024).toFixed(1); setMeter('commit-meter', commitPct);
  $('#commit-detail').textContent = `${commitPct.toFixed(0)}% of the ${(m.commit_limit_mb / 1024).toFixed(0)} GB limit (RAM + page file)`;
  if (currentPage !== 'monitor') return;
  sparkline($('#cpu-spark'), sysHistory.slice(-40).map((h) => h.cpu), cssVar('--series-1'));
  const now = sysHistory[sysHistory.length - 1].t;
  lineChart($('#sys-chart'), { xs: sysHistory.map((h) => h.t - now), yMax: 100, xFormat: (x) => (x === 0 ? 'now' : `${x.toFixed(0)} s`),
    series: [{ name: 'CPU', unit: '%', color: cssVar('--series-1'), values: sysHistory.map((h) => h.cpu) }, { name: 'Memory', unit: '%', color: cssVar('--series-2'), values: sysHistory.map((h) => h.mem) }] });
  if (!liveRunning) { $('#gauge').classList.remove('stall'); $('#gauge').style.setProperty('--v', m.cpu_percent.toFixed(0)); $('#gauge-value').textContent = `${fmt(m.cpu_percent, 0)}%`; $('#gauge-label').textContent = 'CPU in use'; }
}
async function refreshProcesses() {
  if (currentPage !== 'monitor') return;
  let rows; try { rows = await getJSON('/api/processes?sort=cpu'); } catch { return; }
  const top = rows.slice(0, 7); const max = Math.max(5, ...top.map((r) => r.cpu_percent));
  $('#processes').innerHTML = top.map((r) => `<li><span class="pname" title="${esc(r.name)} (PID ${r.pid})">${esc(r.name || 'unknown')}<small>${Math.round(r.rss_mb).toLocaleString()} MB</small></span><div class="bar"><i style="width:${(r.cpu_percent / max) * 100}%"></i></div><span class="pval">${fmt(r.cpu_percent, 1)}%</span></li>`).join('') || '<li class="muted">No data</li>';
}

/* ---------- live monitor ---------- */
let liveRunning = false;
async function refreshLive() {
  let s; try { s = await getJSON('/api/live'); } catch { return; }
  liveRunning = s.running;
  $('#live-toggle').textContent = s.running ? 'Stop stall monitoring' : 'Start stall monitoring';
  $('#live-body').hidden = !(s.running && s.trace.length > 0);
  $('#gauge-note').textContent = s.running ? (s.trace.length ? 'Share of the canary\'s time lost to other work (WSI).' : 'Calibrating the canary…') : 'Start stall monitoring to switch this dial to the Windows Stall Index.';
  if (!s.running || !s.trace.length) return;
  const last = s.trace[s.trace.length - 1]; const th = s.thresholds;
  const recent = s.trace.slice(-5).map((r) => r.wsi); const wsi = recent.reduce((a, b) => a + b, 0) / recent.length;
  $('#wsi-value').textContent = fmt(wsi, 1);
  $('#gauge').classList.add('stall'); $('#gauge').style.setProperty('--v', Math.min(100, wsi * 5).toFixed(0)); $('#gauge-value').textContent = `${fmt(wsi, 1)}%`; $('#gauge-label').textContent = 'time lost (WSI)';
  const state = wsi >= th.engage_wsi ? ['crit', 'Stalling: interactive work is being delayed'] : wsi > th.slo_wsi ? ['warn', 'Elevated'] : ['good', 'Calm'];
  $('#wsi-state').className = `chip ${state[0]}`; $('#wsi-state').textContent = state[1];
  // Summarise the last ~2 s: one 200 ms period can dip even while stalls continue.
  const window10 = s.trace.slice(-10); const counts = {};
  window10.forEach((r) => { if (r.attributed !== 'none') counts[r.attributed] = (counts[r.attributed] || 0) + 1; });
  const top = Object.entries(counts).sort((a, b) => b[1] - a[1])[0];
  const cause = CAUSE[top && wsi >= th.slo_wsi ? top[0] : 'none'];
  $('#cause-value').textContent = cause[0]; $('#cause-why').textContent = cause[1];
  document.querySelectorAll('#ladder li').forEach((li) => li.classList.toggle('on', Number(li.dataset.level) === s.would_level));
  const t0 = s.trace[0].t; const xs = s.trace.map((r) => r.t - t0);
  const shade = []; let start = null;
  s.trace.forEach((r, i) => { if (r.engaged && start == null) start = xs[i]; if ((!r.engaged || i === s.trace.length - 1) && start != null) { shade.push([start, xs[i]]); start = null; } });
  lineChart($('#wsi-chart'), {
    xs, yLabel: 'WSI %', yMax: Math.max(10, ...s.trace.map((r) => r.wsi)) * 1.05, shade,
    thresholds: [{ y: th.engage_wsi }, { y: th.slo_wsi, dash: '1 3' }], xFormat: (x) => `${(x - xs[xs.length - 1]).toFixed(0)} s`,
    series: [{ name: 'WSI', unit: '%', color: cssVar('--series-1'), values: s.trace.map((r) => r.wsi), extra: (i) => `cause: ${(CAUSE[s.trace[i].attributed] || CAUSE.none)[0]}` }],
  });
  const cal = s.calibration; const med = {};
  for (const k of ['sched', 'cpu', 'mem', 'io']) { const vals = window10.map((r) => (r.median_ms || {})[k]).filter((v) => v != null); med[k] = vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null; }
  const rows = [['sched', 'Scheduling delay', 'waiting for a core'], ['cpu', 'Compute step', 'time-slice sharing'], ['mem', 'Memory stream', 'cache and bandwidth'], ['io', 'Storage read', 'disk queue']];
  $('#components').innerHTML = rows.map(([k, name, hint]) => {
    const ratio = cal[k] > 0 ? med[k] / cal[k] : 1; const width = Math.min(100, (ratio / 6) * 100);
    return `<div class="comp"><div class="name"><b>${name}</b><span>${hint} · ${fmt(med[k], 2)} ms (idle ${fmt(cal[k], 2)})</span></div><div class="bar ${ratio >= 2 ? 'hot' : ''}" role="img" aria-label="${name} ${ratio.toFixed(1)} times idle"><i style="width:${width}%"></i></div><div class="ratio">${ratio.toFixed(1)}×</div></div>`;
  }).join('');
}
$('#live-toggle').addEventListener('click', async () => {
  const button = $('#live-toggle'); button.disabled = true;
  try {
    if (liveRunning) { await post('/api/live/stop'); toast('Monitoring stopped'); }
    else { button.textContent = 'Calibrating (about 15 s)…'; await post('/api/live/start'); toast('Stall monitoring started'); }
  } catch (error) { toast(error.message); }
  button.disabled = false; refreshLive();
});

/* ---------- experiment ---------- */
function choice(name, value, title, text, checked) {
  return `<label class="choice"><input type="radio" name="${name}" value="${value}" ${checked ? 'checked' : ''}><b>${esc(title)}</b><span>${esc(text)}</span></label>`;
}
function renderChoices() {
  $('#preset-choices').innerHTML = ['cpu', 'membw', 'io', 'mixed', 'idle'].map((k, i) => choice('preset', k, NAMES[k], PRESET_INFO[k], i === 0)).join('');
  $('#policy-choices').innerHTML = ['adaptive', 'nice', 'static', 'none', 'observe'].map((k, i) => choice('policy', k, NAMES[k], POLICY_INFO[k], i === 0)).join('');
}
const formValue = (name) => document.querySelector(`input[name="${name}"]:checked`)?.value;
let busy = false;
function setBusy(on, label = '', seconds = 0) {
  busy = on; ['#run-trial', '#run-paired'].forEach((s) => { $(s).disabled = on; });
  $('#trial-status').textContent = label; $('#progress').hidden = !on;
  clearInterval(setBusy.timer);
  if (on && seconds) { const started = Date.now(); setBusy.timer = setInterval(() => { $('#progress i').style.width = `${Math.min(97, ((Date.now() - started) / 1000 / seconds) * 100)}%`; }, 250); }
  if (!on) $('#progress i').style.width = '0';
}
$('#trial-form').addEventListener('submit', async (event) => {
  event.preventDefault(); if (busy) return;
  const preset = formValue('preset'); const policy = formValue('policy');
  try {
    setBusy(true, `Running ${NAMES[preset]} × ${NAMES[policy]}…`, 12);
    const run = await post('/api/experiments', { mode: policy === 'none' ? 'baseline' : 'optimized', preset, policy });
    let detail;
    for (;;) { await new Promise((r) => setTimeout(r, 1000)); detail = await getJSON(`/api/runs/${run.run_id}`); if (detail.run.ended_at) break; }
    showTrial(detail); loadRuns();
  } catch (error) { toast(error.message); }
  setBusy(false);
});

function parseEvent(e) {
  const m = /^t=([\d.]+)s value=(\S*) target=(.*?)(?: (.*))?$/.exec(e.detail || '') || [];
  return { t: m[1], value: m[2], target: m[3], reason: m[4] || '' };
}
const ACTION_TEXT = {
  priority_idle: 'Lowered background CPU priority to Idle', io_priority_very_low: 'Lowered background I/O priority to Very low', job_assign: 'Placed background in a Job Object',
  cpu_rate_cap: 'Set background CPU-rate cap', foreground_above_normal: 'Raised foreground priority', cpu_rate_cap_revert: 'Removed CPU-rate cap', priority_revert: 'Restored background CPU priority', io_priority_revert: 'Restored background I/O priority',
};
function showTrial(detail) {
  const r = detail.run; const box = $('#trial-result'); box.hidden = false;
  const events = detail.events.filter((e) => ACTION_TEXT[e.kind]);
  const bg = Object.entries(detail.background_units_per_s || {}).map(([k, v]) => `${k} ${Math.round(v).toLocaleString()}/s`).join(' · ') || 'none';
  box.innerHTML = `<div class="card-head"><h2>${esc(NAMES[r.preset] || r.preset)} × ${esc(NAMES[r.policy] || r.policy)}</h2><span class="muted small">${new Date(r.started_at * 1000).toLocaleTimeString()}</span></div>
  ${r.error ? `<p class="chip crit">${esc(r.error)}</p>` : ''}
  <div class="metrics">
    <div class="metric"><span>Foreground p95</span><b>${fmt(r.foreground_p95_ms, 2)} ms</b><small>p50 ${fmt(r.foreground_p50_ms, 2)} · p99 ${fmt(r.foreground_p99_ms, 2)}</small></div>
    <div class="metric"><span>Deadline misses</span><b>${r.missed_deadlines ?? '–'}</b><small>beats over 25 ms of ${r.operations ?? '–'}</small></div>
    <div class="metric"><span>Background work done</span><b class="small">${esc(bg)}</b><small>units per second in the window</small></div>
    <div class="metric"><span>Actions verified</span><b>${r.verified_count ?? 0} / ${r.action_count ?? 0}</b><small>${r.time_to_first_action_s != null ? `first after ${fmt(r.time_to_first_action_s, 2)} s` : 'no action taken'}</small></div>
  </div>
  ${detail.trace.length ? `<div class="card-head"><h2>Controller timeline</h2><div class="legend"><span><i class="sw s1"></i>WSI %</span><span><i class="sw s2"></i>CPU cap %</span><span><i class="sw dash"></i>engage</span></div></div><div class="chart short" id="trial-chart"></div>` : ''}
  <h2 style="margin:16px 0 8px">Action ledger</h2>
  ${events.length ? `<ul class="ledger">${events.map((e) => { const p = parseEvent(e); return `<li><span class="${e.verified ? 'ok' : 'no'}" title="${e.verified ? 'verified by kernel read-back' : 'NOT verified'}">${e.verified ? '✓' : '✗'}</span><time>${p.t ? `${p.t} s` : ''}</time><div>${esc(ACTION_TEXT[e.kind])}${e.kind === 'cpu_rate_cap' ? ` to ${esc(p.value)}%` : ''}<span class="why">${esc(p.reason)}</span></div></li>`; }).join('')}</ul>` : '<p class="muted">No actions. Either the policy never acts, or no stall was detected.</p>'}`;
  if (detail.trace.length) {
    const t0 = detail.trace[0].t_ms; const xs = detail.trace.map((s) => (s.t_ms - t0) / 1000);
    lineChart($('#trial-chart'), { xs, yMax: 100, thresholds: [{ y: 5 }], xFormat: (x) => `${x.toFixed(1)} s`,
      series: [{ name: 'WSI', unit: '%', color: cssVar('--series-1'), values: detail.trace.map((s) => Math.min(100, s.wsi)), extra: (i) => `level ${detail.trace[i].level}, cause ${detail.trace[i].attributed}` },
        { name: 'CPU cap', unit: '%', color: cssVar('--series-2'), values: detail.trace.map((s) => (s.cap == null ? 100 : s.cap)) }] });
  }
  box.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function loadRuns() {
  let rows; try { rows = await getJSON('/api/runs'); } catch { return; }
  rows = rows.filter((r) => r.engine_version === 2).slice(0, 15);
  $('#runs-table tbody').innerHTML = rows.length ? rows.map((r) => `<tr><td>${new Date(r.started_at * 1000).toLocaleTimeString()}</td><td>${esc(NAMES[r.preset] || r.preset)}</td><td>${esc(NAMES[r.policy] || r.policy)}</td><td class="num">${r.error ? 'error' : r.ended_at ? fmt(r.foreground_p95_ms, 2) : 'running'}</td><td class="num">${r.verified_count ?? 0}/${r.action_count ?? 0}</td><td><button class="btn ghost small" data-run="${r.id}" type="button">View</button></td></tr>`).join('') : '<tr><td colspan="6" class="muted">No trials yet. Pick a workload and policy above.</td></tr>';
  document.querySelectorAll('[data-run]').forEach((b) => b.addEventListener('click', async () => showTrial(await getJSON(`/api/runs/${b.dataset.run}`))));
}
$('#refresh-runs').addEventListener('click', loadRuns);

$('#run-paired').addEventListener('click', async () => {
  if (busy) return;
  const preset = formValue('preset'); const policy = formValue('policy');
  if (policy === 'none') { toast('Pick a policy other than "No mitigation" to compare against it.'); return; }
  try {
    const start = await post('/api/paired-comparison', { repetitions: 5, preset, policy });
    setBusy(true, `5 paired trials of ${NAMES[preset]}: ${NAMES[policy]} vs none…`, 130);
    for (;;) {
      await new Promise((r) => setTimeout(r, 4000));
      const done = (await getJSON('/api/runs')).filter((r) => r.batch_id === start.batch_id && r.ended_at).length;
      $('#trial-status').textContent = `Pair trials finished: ${done} / 10`;
      if (done >= 10) break;
    }
    renderComparison(await getJSON('/api/comparison')); loadRuns();
  } catch (error) { toast(error.message); }
  setBusy(false);
});

function renderComparison(c) {
  const box = $('#comparison-data'); box.hidden = false;
  const cls = (c.classification || 'INSUFFICIENT_DATA').toLowerCase(); const t = c.paired_tests || {};
  box.className = `card ${cls}`;
  box.innerHTML = `<div class="card-head"><h2>Paired comparison: ${esc(NAMES[c.preset] || c.preset)}, ${esc(NAMES[c.policy] || c.policy)} vs no mitigation</h2></div>
  <div class="verdict"><span class="badge">${esc(c.classification.replaceAll('_', ' '))}</span><span class="muted small">${c.paired_repetitions} pairs, alternating order · ${c.valid_paired_repetitions} with real interference</span></div>
  <div class="metrics">
    <div class="metric"><span>No mitigation, mean p95</span><b>${fmt(c.baseline.mean_ms, 2)} ms</b></div>
    <div class="metric"><span>With policy, mean p95</span><b>${fmt(c.optimized.mean_ms, 2)} ms</b></div>
    <div class="metric"><span>Change</span><b>${c.percentage_difference == null ? '–' : `${c.percentage_difference > 0 ? '+' : ''}${fmt(c.percentage_difference, 1)}%`}</b><small>95% CI ${t.t_ci95_ms ? `${fmt(t.t_ci95_ms[0], 2)} to ${fmt(t.t_ci95_ms[1], 2)} ms` : '–'}</small></div>
    <div class="metric"><span>Exact Wilcoxon p</span><b>${pval(t.wilcoxon_p)}</b></div>
    <div class="metric"><span>Background work kept</span><b>${pct(c.background_throughput_retained_median)}</b><small>the cost of protection</small></div>
  </div>
  <p class="conclusion">${esc(c.plain_language_conclusion)}</p>`;
}

/* ---------- evidence ---------- */
let lastFeedTop = null;
async function loadStudyProgress() {
  let p; try { p = await getJSON('/api/study/progress'); } catch { return; }
  const box = $('#study-progress');
  if (!p.active) { box.innerHTML = ''; return; }
  const share = p.done / p.total; const fresh = p.recent[0] && p.recent[0].started_at !== lastFeedTop; lastFeedTop = p.recent[0]?.started_at;
  box.innerHTML = `<article class="card progress-card"><div class="card-head"><h2>Study running now: ${esc(p.study_id)}</h2><span class="chip warn">In progress</span></div>
    <p class="muted small">${esc(p.host.cpu)} · ${p.host.logical_cpus} logical CPUs · ${p.host.ram_gb} GB RAM. Trials run in random order inside each block.</p>
    <div class="bigbar" role="progressbar" aria-valuemin="0" aria-valuemax="${p.total}" aria-valuenow="${p.done}"><i style="width:${(share * 100).toFixed(1)}%"></i></div>
    <p><b>${p.done}</b> of ${p.total} trials (${(share * 100).toFixed(0)}%) · about ${Math.round(((p.total - p.done) * 11) / 60)} min left</p>
    <h2 style="margin-top:16px">Latest trials</h2>
    <ul class="feed">${p.recent.map((t, i) => `<li class="${i === 0 && fresh ? 'fresh' : ''}"><span class="muted">block ${t.block + 1}</span><span>${esc(NAMES[t.preset] || t.preset)}</span><span>${esc(NAMES[t.policy] || t.policy)}</span><span class="num">p95 ${fmt(t.p95_ms, 1)} ms</span><span class="num muted">${t.verified}/${t.actions} verified</span></li>`).join('')}</ul></article>`;
}

/* ---------- evidence results ---------- */
async function loadResearch() {
  const box = $('#research-data');
  let s; try { s = await getJSON('/api/study'); } catch { box.innerHTML = $('#study-progress').innerHTML ? '<p class="muted small">Results, confidence intervals and figures appear here as soon as the study finishes and is analysed.</p>' : '<div class="empty"><h2>No study analysed yet</h2><p>Run <code>python -m research.run_study</code>, then <code>python -m research.analyze</code>. Results appear here automatically.</p></div>'; return; }
  const main = s.comparisons.filter((c) => c.metric === 'p95_ms' && c.reference === 'none' && ['nice', 'static', 'adaptive'].includes(c.policy));
  const vsV1 = s.comparisons.filter((c) => c.metric === 'p95_ms' && c.policy === 'adaptive' && c.reference !== 'none');
  const abl = s.comparisons.filter((c) => c.metric === 'p95_ms' && c.reference === 'adaptive');
  const row = (c, label = NAMES[c.policy]) => { const win = c.ci_high < 1 && c.p_holm < 0.05; const loss = c.ci_low > 1 && c.p_holm < 0.05; return `<tr class="${win ? 'win' : loss ? 'loss' : ''}"><td>${esc(NAMES[c.preset])}</td><td>${esc(label)}</td><td class="num">${fmt(c.median_a, 2)}</td><td class="num">${fmt(c.median_ratio, 2)} <span class="muted">[${fmt(c.ci_low, 2)}, ${fmt(c.ci_high, 2)}]</span></td><td class="num ${c.p_holm < 0.05 ? 'sig' : ''}">${pval(c.p_holm)}</td><td class="num">${pct(c.bg_retained_median)}</td></tr>`; };
  const table = (rows, ref, label) => `<div class="table-wrap"><table class="table"><thead><tr><th>Workload</th><th>Policy</th><th class="num">p95 ms</th><th class="num">p95 ratio vs ${ref} [95% CI]</th><th class="num">p (Holm)</th><th class="num">BG work kept</th></tr></thead><tbody>${rows.map((c) => row(c, label ? label(c) : undefined)).join('')}</tbody></table></div>`;
  const v = s.validity; const acc = s.attribution_accuracy; const led = s.ledger;
  const accs = Object.values(acc).filter((a) => a.accuracy != null);
  box.innerHTML = `<div class="findings">
    <div class="finding"><b>${s.trials}</b><p>trials in ${s.blocks} randomised blocks on ${esc(s.host.cpu)} (${s.trials_with_errors} with errors)</p></div>
    <div class="finding"><b>${Object.values(v).map((x) => `${fmt(x.inflation, 1)}×`).join(' / ')}</b><p>p95 inflation from CPU / memory / storage / mixed interference vs. the no-interference control</p></div>
    <div class="finding"><b>${accs.length ? pct(accs.reduce((a, b) => a + b.correct, 0) / accs.reduce((a, b) => a + b.periods_above_threshold, 0)) : '–'}</b><p>of stalled control periods attributed to the right resource (observe-only runs)</p></div>
    <div class="finding"><b>${led.actions_verified}/${led.actions}</b><p>actions verified by kernel read-back; ${led.reverts_verified}/${led.reverts} reverts verified; ${led.cap_rollbacks} ineffective caps rolled back</p></div>
  </div>
  <article class="card"><div class="card-head"><h2>Foreground tail latency vs. no mitigation</h2><span class="muted small">ratio &lt; 1 is better; marked rows: CI excludes 1 and p &lt; 0.05</span></div>${table(main, 'none')}</article>
  <article class="card"><div class="card-head"><h2>SmartSwap v2 vs. the alternatives</h2></div>${table(vsV1, 'reference', (c) => `v2 vs ${NAMES[c.reference]}`)}</article>
  <article class="card"><div class="card-head"><h2>Ablations (each component removed)</h2><span class="muted small">ratio &gt; 1: removing it made things worse</span></div>${table(abl, 'full v2')}</article>
  <article class="card"><h2 style="margin-bottom:12px">Figures</h2><div class="figures">
    <figure><img alt="Protection versus cost per workload and policy" src="/api/study/figures/fig_tradeoff.png"><figcaption>Protection vs. cost: up and to the right is better.</figcaption></figure>
    <figure><img alt="Controller timelines" src="/api/study/figures/fig_controller_timeline.png"><figcaption>Controller behaviour in a typical trial of each workload.</figcaption></figure>
    <figure><img alt="Foreground p95 per block" src="/api/study/figures/fig_p95_by_policy.png"><figcaption>Every block's p95, by policy.</figcaption></figure>
  </div></article>`;
}

/* ---------- reports ---------- */
async function loadCaps() {
  const caps = await getJSON('/api/capabilities').catch(() => null); if (!caps) return;
  const entries = Object.entries(caps.apis);
  $('#api-count').textContent = `${entries.filter(([, ok]) => ok).length} of ${entries.length} available`;
  $('#caps').innerHTML = entries.map(([name, ok]) => `<div class="${ok ? '' : 'missing'}">${esc(name)}</div>`).join('');
}
$('#cleanup').addEventListener('click', async () => { try { const r = await post('/api/cleanup'); toast(r.message); } catch (error) { toast(error.message); } });

/* ---------- boot ---------- */
renderChoices();
refreshSystem(); refreshLive(); refreshProcesses();
setInterval(refreshSystem, 1000);
setInterval(refreshProcesses, 3000);
setInterval(() => { if (currentPage === 'evidence') loadStudyProgress(); }, 5000);
setInterval(() => { if (currentPage === 'monitor') refreshLive(); }, 1000);
window.addEventListener('resize', () => { if (currentPage === 'monitor') refreshLive(); });
navigate(location.hash.slice(1) || 'monitor');
