/* Rendering reads saved trial state. API collection runs in the Python worker. */
const element = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? '—').replace(/[&<>"']/g, (character) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
})[character]);
const decimal = (value) => value == null ? '—' : Number(value).toFixed(1);
const clock = (seconds) => seconds == null ? '—' : `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
const periodLabel = (period) => period == null ? '—' : period <= 4 ? `Q${period}` : `OT ${period - 4}`;
const timeLabel = (timestamp) => timestamp ? new Date(timestamp * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'}) : '—';
const units = (value) => `${value >= 0 ? '+' : ''}${Number(value).toFixed(2)}u`;
let currentFilter = 'all';
let latestState;

function outcome(row) {
  const labels = {
    hit: row.picked ? 'Hit' : 'Would hit',
    miss: row.picked ? 'Miss' : 'Would miss',
    push: row.picked ? 'Push' : 'Would push',
    pending: 'Awaiting final',
    unavailable: 'No in-window line'
  };
  const color = row.line_result === 'hit' ? 'positive' : row.line_result === 'miss' ? 'negative' : 'neutral';
  const profit = row.picked && row.pnl_units != null ? ` · ${units(row.pnl_units)}` : '';
  return `<span class="outcome ${color}">${labels[row.line_result] ?? 'Awaiting final'}${profit}</span><small>${row.picked ? 'Picked' : 'Not picked'}${row.final_total == null ? '' : ` · Final ${row.final_total}`}</small>`;
}

function renderDecisions(rows) {
  const filtered = rows.filter((row) => currentFilter === 'all' ||
    (currentFilter === 'picked' && row.picked) ||
    (currentFilter === 'skipped' && ['SKIP', 'BORDERLINE'].includes(row.decision)) ||
    (currentFilter === 'missed' && row.decision === 'MISSED'));
  const signals = {PLAY_UNDER: 'PLAY UNDER', SKIP: 'SKIP', BORDERLINE: 'BORDERLINE', MISSED: 'MISSED'};
  element('decisions').innerHTML = filtered.length ? filtered.map((row) => {
    const missed = row.decision === 'MISSED';
    return `<tr>
      <td>${escapeHtml(row.name)}<small>${timeLabel(row.at)}</small></td>
      <td><span class="signal ${row.decision}" title="${escapeHtml(row.reason)}">${signals[row.decision]}</span></td>
      <td class="mono">${clock(row.seconds_left)}</td>
      <td class="mono">${row.home_score ?? '—'}–${row.away_score ?? '—'}</td>
      <td class="mono">${missed ? '—' : decimal(row.total_line)}</td>
      <td class="mono">${missed ? '—' : decimal(row.points_offered)}</td>
      <td class="mono">${missed || row.under_odds_decimal == null ? '—' : row.under_odds_decimal.toFixed(2)}</td>
      <td>${outcome(row)}</td>
    </tr>`;
  }).join('') : '<tr><td class="empty" colspan="8">No decisions in this view yet.</td></tr>';
}

function renderEquity(decisions) {
  const settled = decisions.filter((row) => row.picked && row.pnl_units != null).sort((a, b) => a.at - b.at);
  const values = [0];
  settled.forEach((row) => values.push(values[values.length - 1] + row.pnl_units));
  const low = Math.min(-1, ...values) - 0.3;
  const high = Math.max(1, ...values) + 0.3;
  const width = 600;
  const height = 120;
  const y = (value) => 12 + (high - value) / (high - low) * 90;
  const points = values.map((value, index) => `${index / Math.max(1, values.length - 1) * width},${y(value)}`);
  const path = `M${points.join(' L')}`;
  const shade = `${path} L600,${height} L0,${height} Z`;
  const last = values[values.length - 1];
  element('equity').innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="${settled.length} settled picks, cumulative return ${units(last)}">
    <defs><linearGradient id="fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#8fad80" stop-opacity=".22"/><stop offset="100%" stop-color="#8fad80" stop-opacity="0"/></linearGradient></defs>
    <line x1="0" y1="${y(0)}" x2="600" y2="${y(0)}" stroke="#dce4d5" stroke-dasharray="3 5"/>
    <path d="${shade}" fill="url(#fill)"/><path d="${path}" fill="none" stroke="#538147" stroke-width="2" vector-effect="non-scaling-stroke"/>
    <circle cx="${values.length > 1 ? width : 0}" cy="${y(last)}" r="3" fill="#538147"/>
  </svg>`;
  element('settled-count').textContent = `${settled.length} settled picks`;
}

function render(state) {
  latestState = state;
  const totals = state.totals;
  const demo = state.mode === 'demo';
  const active = state.events.filter((event) => event.result_status !== 'final' && state.now - event.last_seen < 90);
  element('demo-banner').hidden = !demo;
  element('connection').textContent = demo ? '● Synthetic replay running' : state.status === 'complete' ? 'Trial complete' : '● ' + state.status;
  element('error').hidden = !state.last_error;
  element('error').textContent = state.last_error;
  element('games').textContent = state.tracked_games;
  element('active-count').textContent = active.length;
  element('picks').textContent = totals.picks || 0;
  element('pending').textContent = totals.pending || 0;
  const resolved = (totals.wins || 0) + (totals.losses || 0);
  element('win-rate').textContent = resolved ? `${((totals.wins || 0) / resolved * 100).toFixed(1)}%` : '—';
  element('record').textContent = `${totals.wins || 0} hits / ${totals.losses || 0} misses / ${totals.pushes || 0} pushes`;
  element('pnl').textContent = totals.pnl_units == null ? '—' : units(totals.pnl_units);
  element('pnl').className = totals.pnl_units < 0 ? 'negative' : 'positive';
  element('calls-hour').textContent = state.calls_hour.toLocaleString();
  element('hourly-budget').textContent = state.hourly_budget.toLocaleString();
  element('budget-bar').style.width = `${Math.min(100, state.calls_hour / state.hourly_budget * 100)}%`;
  element('provider-remaining').textContent = demo ? 'Offline replay' : state.provider_remaining?.toLocaleString() ?? '—';
  element('provider-reset').textContent = demo ? 'Offline replay' : timeLabel(state.provider_reset);
  element('last-update').textContent = timeLabel(state.last_success);
  const minutes = Math.max(0, Math.ceil((state.trial_end - state.now) / 60));
  element('trial-clock').textContent = demo ? 'Synthetic replay · 2 second ticks' : state.status === 'complete' ? 'Trial complete' : `${Math.floor(minutes / 60)}h ${minutes % 60}m remaining`;
  element('decision-count').textContent = totals.decisions || 0;
  renderDecisions(state.decisions);
  renderEquity(state.decisions);
  element('events').innerHTML = active.length ? active.slice(0, 8).map((event) => `<tr><td>${escapeHtml(event.name)}</td><td class="mono">${periodLabel(event.period)}</td><td class="mono">${clock(event.seconds_left)}</td><td class="mono">${event.home_score ?? '—'}–${event.away_score ?? '—'}</td></tr>`).join('') : '<tr><td class="empty" colspan="4">No active games in the feed.</td></tr>';
  element('observation-rows').innerHTML = state.observations.length ? state.observations.map((row) => `<tr><td class="mono">${timeLabel(row.at)}</td><td>${escapeHtml(row.name)}</td><td class="mono">${periodLabel(row.period)}</td><td class="mono">${clock(row.seconds_left)}</td><td class="mono">${row.home_score ?? '—'}–${row.away_score ?? '—'}</td><td class="mono">${decimal(row.total_line)}</td><td class="mono">${decimal(row.points_offered)}</td></tr>`).join('') : '<tr><td class="empty" colspan="7">Q4 snapshots will appear here.</td></tr>';
}

document.querySelectorAll('[data-filter]').forEach((button) => {
  button.addEventListener('click', () => {
    currentFilter = button.dataset.filter;
    document.querySelectorAll('[data-filter]').forEach((other) => {
      const active = other === button;
      other.classList.toggle('active', active);
      other.setAttribute('aria-pressed', active);
    });
    if (latestState) renderDecisions(latestState.decisions);
  });
});

async function refresh() {
  try {
    const response = await fetch('/api/state', {cache: 'no-store'});
    if (!response.ok) throw new Error('State unavailable');
    render(await response.json());
  } catch {
    element('connection').textContent = 'Reconnecting…';
    element('error').hidden = false;
    element('error').textContent = 'The dashboard could not reach the local trial. Retrying shortly.';
  } finally {
    setTimeout(refresh, 2000);
  }
}
refresh();
