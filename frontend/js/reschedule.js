/* Rescheduling page: report floor state, disruptions and rush orders, then
   re-solve from the freeze point with minimum perturbation. */

let problem = null;
let baseline = null;
let progress = [];           // [{task, state, processed, remaining, actual_start}]
let breakdowns = [];         // [{resource, start, end}]
let rushTasks = [];          // [{id, duration, resource_requirements}]
let lastSolution = null;

initSidebar('reschedule.html');

async function load() {
  const id = currentProblemId();
  if (!id) return;
  problem = await api('/problems/' + id);
  document.getElementById('now').value = 0;
  fillResourceSelectors();
  await loadBaselines();
}

function fillResourceSelectors() {
  const opts = problem.resources.map(r =>
    `<option value="${r.id}">${escapeHtml(r.id)} · ${escapeHtml(r.name || '')}</option>`).join('');
  document.getElementById('bd-res').innerHTML = opts;
  document.getElementById('rush-res').innerHTML = opts;
}

async function loadBaselines() {
  const { solutions } = await api(`/problems/${problem.id}/solutions`);
  const sel = document.getElementById('baseline-picker');
  // reschedules chain naturally: the newest plan is usually the active one
  const ordered = solutions.slice().reverse();
  sel.innerHTML = ordered.length
    ? ordered.map(s => {
        const tag = s.solver === 'stable' ? '🔁 ' : '';
        return `<option value="${s.id}">${tag}${escapeHtml(SOLVER_LABELS[s.solver] || s.solver)} · 目标 ${fmt(s.objective_value)} · ${escapeHtml(s.created_at)}</option>`;
      }).join('')
    : '<option value="">— 请先在求解器页生成一个方案 —</option>';
  if (ordered.length) await loadBaseline(ordered[0].id);
}

document.getElementById('baseline-picker')
  .addEventListener('change', e => loadBaseline(e.target.value));

async function loadBaseline(id) {
  baseline = await api(`/problems/${problem.id}/solutions/${id}`);
  const now = Number(document.getElementById('now').value) || 0;
  applyDerived(await fetchPlan(now));
}

async function fetchPlan(now) {
  return api(`/problems/${problem.id}/reschedule/plan`, {
    method: 'POST',
    body: JSON.stringify({ baseline_solution_id: baseline.id, now }),
  });
}

async function derive() {
  if (!baseline) { toast('请先选择基准方案', 'error'); return; }
  const now = Number(document.getElementById('now').value);
  if (!Number.isFinite(now) || now < 0) { toast('当前时点必须 ≥ 0', 'error'); return; }
  const plan = await fetchPlan(now);
  // preserve manual overrides only where they remain meaningful; simplest and
  // least surprising is to re-derive fully — the button says so.
  applyDerived(plan);
  toast('已按 t=' + now + ' 重新判定任务状态');
}

function applyDerived(plan) {
  const tmap = Object.fromEntries(problem.tasks.map(t => [t.id, t]));
  progress = plan.progress.map(p => {
    const t = tmap[p.task] || {};
    return {
      task: p.task,
      state: p.state,
      actual_start: p.actual_start,
      processed: p.processed || 0,
      remaining: p.remaining !== null && p.remaining !== undefined
        ? p.remaining : (t.duration - (p.processed || 0)),
    };
  });
  renderProgress();
}

const BASE_OF = () => {
  const m = {};
  for (const a of baseline.assignments) m[a.task] = a;
  return m;
};

function renderProgress() {
  const base = BASE_OF();
  const tb = document.getElementById('progress-rows');
  tb.innerHTML = progress.map((p, i) => {
    const b = base[p.task];
    const editable = p.state === 'in_progress' || p.state === 'interrupted';
    return `<tr>
      <td class="mono">${escapeHtml(p.task)}</td>
      <td><select data-i="${i}" data-field="state" onchange="onProgressChange(this)">
        ${['completed', 'in_progress', 'interrupted', 'pending'].map(s =>
          `<option value="${s}" ${s === p.state ? 'selected' : ''}>${progressLabel(s)}</option>`).join('')}
      </select></td>
      <td class="num">${b ? b.start : '—'}</td>
      <td class="num">${b ? b.end : '—'}</td>
      <td><input type="number" min="0" style="width:70px" data-i="${i}" data-field="processed"
          value="${p.processed ?? ''}" ${editable ? '' : 'disabled'}
          onchange="onProgressChange(this)"></td>
      <td><input type="number" min="0" style="width:70px" data-i="${i}" data-field="remaining"
          value="${p.remaining ?? ''}" ${editable ? '' : 'disabled'}
          onchange="onProgressChange(this)"></td>
    </tr>`;
  }).join('');
}

function onProgressChange(el) {
  const i = Number(el.dataset.i);
  const field = el.dataset.field;
  progress[i][field] = field === 'state' ? el.value : Number(el.value);
  if (field === 'state') renderProgress();
}

/* ---- breakdowns ------------------------------------------------------ */
function addBreakdown() {
  const res = document.getElementById('bd-res').value;
  const s = Number(document.getElementById('bd-start').value);
  const e = Number(document.getElementById('bd-end').value);
  if (!Number.isFinite(s) || !Number.isFinite(e) || e <= s) {
    toast('故障窗口无效（开始需小于结束）', 'error'); return;
  }
  breakdowns.push({ resource: res, start: s, end: e });
  document.getElementById('bd-start').value = '';
  document.getElementById('bd-end').value = '';
  renderBreakdowns();
}

function removeBreakdown(i) { breakdowns.splice(i, 1); renderBreakdowns(); }

function renderBreakdowns() {
  const box = document.getElementById('breakdown-rows');
  box.innerHTML = breakdowns.length
    ? breakdowns.map((b, i) =>
        `<span class="badge warn" style="margin:2px">⛔ ${escapeHtml(b.resource)}：[${b.start}, ${b.end})
           <a href="#" onclick="removeBreakdown(${i});return false" style="margin-left:6px">✕</a></span>`).join('')
    : '<span class="muted">暂无故障登记。</span>';
}

/* ---- rush tasks ------------------------------------------------------ */
function addRush() {
  const id = document.getElementById('rush-id').value.trim();
  const dur = Number(document.getElementById('rush-dur').value);
  const res = document.getElementById('rush-res').value;
  if (!id) { toast('请填写急单任务号', 'error'); return; }
  if (!Number.isFinite(dur) || dur <= 0) { toast('工期必须为正整数', 'error'); return; }
  if (problem.tasks.some(t => t.id === id) || rushTasks.some(t => t.id === id)) {
    toast('任务号已存在：' + id, 'error'); return;
  }
  rushTasks.push({ id, duration: dur, resource_requirements: { [res]: 1 },
                   dependencies: [], release_time: Number(document.getElementById('now').value) || 0 });
  document.getElementById('rush-id').value = '';
  document.getElementById('rush-dur').value = '';
  renderRush();
}

function removeRush(i) { rushTasks.splice(i, 1); renderRush(); }

function renderRush() {
  const box = document.getElementById('rush-rows');
  box.innerHTML = rushTasks.length
    ? rushTasks.map((t, i) =>
        `<span class="badge info" style="margin:2px">⚡ ${escapeHtml(t.id)} · 工期 ${t.duration}
           · ${escapeHtml(Object.keys(t.resource_requirements).join(', '))}
           <a href="#" onclick="removeRush(${i});return false" style="margin-left:6px">✕</a></span>`).join('')
    : '<span class="muted">暂无急单。</span>';
}

/* ---- run ------------------------------------------------------------- */
async function runReschedule() {
  if (!baseline) { toast('请先选择基准方案', 'error'); return; }
  const now = Number(document.getElementById('now').value);
  const btn = document.getElementById('run-btn');
  btn.disabled = true; btn.textContent = '重排中…';
  try {
    const body = {
      baseline_solution_id: baseline.id,
      now,
      progress: progress.map(p => ({
        task: p.task,
        state: p.state,
        actual_start: p.actual_start ?? null,
        processed: Number(p.processed) || 0,
        remaining: p.remaining === '' || p.remaining === null || p.remaining === undefined
          ? null : Number(p.remaining),
      })),
      breakdowns,
      rush_tasks: rushTasks.map(t => ({
        id: t.id, duration: t.duration,
        resource_requirements: t.resource_requirements,
        dependencies: t.dependencies,
      })),
      solver: document.getElementById('solver').value,
      stability_weight: Number(document.getElementById('stability-weight').value) || 0,
      persist: true,
    };
    const out = await api(`/problems/${problem.id}/reschedule`,
                          { method: 'POST', body: JSON.stringify(body) });
    lastSolution = out.solution;
    if (out.n_new_tasks) {
      // problem definition changed; reload so the chart knows the rush tasks
      problem = await api('/problems/' + problem.id);
    }
    renderResult(out.solution, now);
    toast(`重排完成：移动 ${out.solution.change_summary.n_moved} 个任务，` +
          `状态 ${statusLabel(out.solution.status)}`);
  } catch (e) {
    toast('重排失败：' + e.message, 'error');
  } finally {
    btn.disabled = false; btn.textContent = '▶ 从现在起重排';
  }
}

function renderResult(sol, now) {
  document.getElementById('result-card').style.display = 'block';
  const cs = sol.change_summary || {};
  document.getElementById('k-done').textContent = cs.n_completed;
  document.getElementById('k-prog').textContent = cs.n_in_progress;
  document.getElementById('k-int').textContent = cs.n_interrupted;
  document.getElementById('k-moved').textContent = cs.n_moved;
  document.getElementById('k-shift').textContent = cs.abs_shift;
  document.getElementById('k-max').textContent = cs.max_shift;

  const meta = document.getElementById('result-meta');
  meta.innerHTML = `${statusBadge(sol.status)}
    <span class="badge ${sol.solver === 'stable' ? 'info' : 'muted'}">${escapeHtml(SOLVER_LABELS[sol.solver] || sol.solver)}</span>
    <span class="badge muted">完工 ${fmt(sol.makespan)}</span>`;

  const objMeta = document.getElementById('obj-meta');
  objMeta.innerHTML = `
    <span class="small muted">原计划目标：<b>${fmt(cs.base_objective)}</b></span>
    <span class="small muted">新计划目标：<b>${fmt(sol.objective_value)}</b></span>
    <span class="small muted">目标变化：<b style="color:${cs.objective_delta > 0 ? 'var(--bad)' : 'var(--ok)'}">${
      cs.objective_delta === null ? '—' : (cs.objective_delta > 0 ? '+' : '') + fmt(cs.objective_delta)}</b></span>
    <span class="small muted">求解耗时：${fmt(sol.solve_time)}s</span>`;

  const rowMap = Object.fromEntries((cs.rows || []).map(r => [r.task, r]));
  const base = BASE_OF();

  renderGantt(document.getElementById('gantt-container'), {
    problem,
    assignments: sol.assignments,
    mode: 'resource',
    title: '重排后甘特图（浅色＝已完成冻结，斜线＝在制/中断，虚线框＝原计划）',
    nowMarker: now,
    statusOf: (tid) => {
      const r = rowMap[tid];
      if (!r) return 'pending';
      if (r.change === 'added') return 'added';
      return r.state;
    },
    baselineOf: (tid) => {
      const r = rowMap[tid];
      if (!r || r.change === 'added') return null;
      return base[tid] || null;
    },
  });
  document.getElementById('legend').innerHTML =
    `<span><span class="swatch" style="background:#9ca3af;opacity:.5"></span>已完成（冻结）</span>` +
    `<span><span class="swatch" style="background:#9ca3af"></span>在制/中断（斜线）</span>` +
    `<span><span class="swatch" style="background:#4e79a7"></span>未开始/急单</span>` +
    `<span><span style="display:inline-block;width:18px;border-top:2px dashed #9ca3af;vertical-align:middle"></span> 原计划位置</span>` +
    `<span><span style="display:inline-block;width:18px;border-top:2px dashed #2563eb;vertical-align:middle"></span> 当前时点 t=${now}</span>`;

  const diff = document.getElementById('diff-rows');
  diff.innerHTML = (cs.rows || []).map(r => {
    const moved = r.change === 'moved' || r.change === 'rescheduled';
    return `<tr${r.change === 'unchanged' ? ' class="muted"' : ''} style="${r.change === 'unchanged' ? 'opacity:.55' : ''}">
      <td class="mono">${escapeHtml(r.task)}</td>
      <td>${progressLabel(r.state)}</td>
      <td><span class="badge ${r.change === 'unchanged' ? 'muted'
        : r.change === 'added' ? 'info'
        : r.change === 'unscheduled' ? 'bad'
        : moved ? 'warn' : 'ok'}">${changeLabel(r.change)}</span></td>
      <td class="num">${r.base_start ?? '—'}</td>
      <td class="num">${r.base_end ?? '—'}</td>
      <td class="num">${r.new_start ?? '—'}</td>
      <td class="num">${r.new_end ?? '—'}</td>
      <td class="num"><b style="color:${moved && r.delta > 0 ? 'var(--bad)' : 'inherit'}">${
        r.delta === null || r.delta === undefined ? '—' : (r.delta > 0 ? '+' + r.delta : r.delta)}</b></td>
    </tr>`;
  }).join('');

  document.getElementById('gantt-link').href =
    `gantt.html?id=${encodeURIComponent(problem.id)}&solution=${encodeURIComponent(sol.id)}`;
}

renderBreakdowns();
load().catch(e => toast(e.message, 'error'));
