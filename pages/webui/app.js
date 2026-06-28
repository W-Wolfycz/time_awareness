/**
 * time_awareness WebUI
 *
 * 4 个 view：概览 / 日历（月视图）/ 任务 / 配置
 * vanilla JS，无框架，无 URL router（DOM 显隐切换）
 */

// ==================== [1] 桥接 + 全局状态 ====================
const PLUGIN_NAME = 'time_awareness';

// 兼容 AstrBot 主 webui 注入的 bridge；缺失时降级 fetch（用相对路径）
const bridge = window.AstrBotPluginPage || null;

const state = {
  currentView: 'dashboard',
  calendar: { year: 0, month: 0 }, // 0 表示尚未初始化（init 时取当前年月）
  stats: null,
  tasks: [],
  about: null,
};

const VIEW_TITLES = {
  dashboard: '概览',
  calendar: '日历',
  tasks: '任务',
};

// ==================== [2] API client ====================
// 桥接模式下 endpoint 不带插件名前缀——父窗口自动拼 /{plugin_name}/<endpoint>
// 降级模式（脱离 AstrBot 直接打开）才拼完整 /api/{plugin_name}/<endpoint>
async function apiGet(endpoint, params = {}) {
  if (bridge && typeof bridge.apiGet === 'function') {
    return bridge.apiGet(endpoint, params);
  }
  // 降级：直接 fetch（仅本地开发调试）
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') qs.set(k, String(v));
  });
  const query = qs.toString();
  const url = `/api/${PLUGIN_NAME}/${endpoint}${query ? '?' + query : ''}`;
  const resp = await fetch(url);
  return resp.json();
}

async function apiPost(endpoint, body = {}) {
  if (bridge && typeof bridge.apiPost === 'function') {
    return bridge.apiPost(endpoint, body);
  }
  const resp = await fetch(`/api/${PLUGIN_NAME}/${endpoint}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return resp.json();
}

const api = {
  getAbout: () => apiGet('about'),
  getStats: () => apiGet('dashboard/stats'),
  getMonth: (year, month) => apiGet('calendar/month', { year, month }),
  getTasks: () => apiGet('tasks/list'),
  cancelTask: (task_id) => apiPost('tasks/cancel', { task_id }),
};

/** 等待桥接 SDK 与父窗口握手完成。降级模式立即返回。 */
async function waitBridgeReady() {
  if (bridge && typeof bridge.ready === 'function') {
    try { await bridge.ready(); } catch (e) { /* 握手失败仍允许尝试调用 */ }
  }
}

// ==================== [3] view switcher ====================
function switchView(name) {
  state.currentView = name;
  document.querySelectorAll('.nav-item').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.view === name);
  });
  document.querySelectorAll('.view').forEach(v => {
    v.classList.toggle('active', v.dataset.view === name);
  });
  document.getElementById('view-title').textContent = VIEW_TITLES[name] || name;
  refreshCurrentView();
}

async function refreshCurrentView() {
  switch (state.currentView) {
    case 'dashboard': return renderDashboard();
    case 'calendar': return renderCalendar();
    case 'tasks': return renderTasks();
  }
}

// ==================== [4] render: dashboard ====================
async function renderDashboard() {
  const wrap = document.getElementById('dashboard-content');
  try {
    const resp = await api.getStats();
    if (!resp.success) throw new Error(resp.error || '获取统计失败');
    state.stats = resp.stats;

    const s = state.stats;
    wrap.innerHTML = `
      <div class="metrics-grid">
        <div class="metric-card">
          <div class="metric-label">自定义事件总数</div>
          <div class="metric-value">${s.custom_event_total}</div>
          <div class="metric-sub">calendar_data.yaml</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">内置事件总数</div>
          <div class="metric-value">${s.builtin_event_total}</div>
          <div class="metric-sub">节日 / 节气 / 黄历</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">本月事件</div>
          <div class="metric-value">${s.this_month_event_count}</div>
          <div class="metric-sub">含 builtin + custom</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">待触发任务</div>
          <div class="metric-value">${s.task_pending_total}</div>
          <div class="metric-sub">日历 ${s.task_calendar_pending} · 后续 ${s.task_followup_pending}</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">近 7 天任务</div>
          <div class="metric-value">${s.near_7d_task_count}</div>
          <div class="metric-sub">即将触发</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">下个任务</div>
          <div class="metric-value" style="font-size: 20px;">${escapeHtml(s.next_task_display)}</div>
          <div class="metric-sub">最近一条 pending</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">日历功能</div>
          <div class="metric-value" style="font-size: 20px;">
            <span class="badge ${s.calendar_enabled ? 'badge-success' : 'badge-warning'}">
              ${s.calendar_enabled ? '已启用' : '未启用'}
            </span>
          </div>
        </div>
        <div class="metric-card">
          <div class="metric-label">主动提醒</div>
          <div class="metric-value" style="font-size: 20px;">
            <span class="badge ${s.reminder_enabled ? 'badge-success' : 'badge-warning'}">
              ${s.reminder_enabled ? '已启用' : '未启用'}
            </span>
          </div>
        </div>
      </div>
    `;
  } catch (e) {
    wrap.innerHTML = `<div class="empty-state">⚠️ ${escapeHtml(e.message)}</div>`;
  }
}

// ==================== [5] render: calendar 月视图 ====================
const WEEKDAY_HEADERS = ['一', '二', '三', '四', '五', '六', '日'];

function getNowInBrowser() {
  const d = new Date();
  return { year: d.getFullYear(), month: d.getMonth() + 1, day: d.getDate() };
}

function formatMonthTitle(year, month) {
  return `${year} 年 ${month} 月`;
}

function renderWeekdayHeader() {
  return WEEKDAY_HEADERS.map(w => `<div class="calendar-weekday">${w}</div>`).join('');
}

/** 算当月 1 号是周几（周一=0，周日=6，国内习惯） */
function firstDayOffset(year, month) {
  const d = new Date(year, month - 1, 1).getDay(); // 0=Sun...6=Sat
  return (d + 6) % 7; // 转为周一开始
}

function daysInMonth(year, month) {
  return new Date(year, month, 0).getDate();
}

function renderCalendarCell(date, events) {
  if (date === null) {
    return `<div class="calendar-cell empty"></div>`;
  }
  const today = getNowInBrowser();
  const isToday = (date === today.day && state.calendar.month === today.month && state.calendar.year === today.year);
  const visible = events.slice(0, 3);
  const more = events.length - visible.length;
  const chips = visible.map(e => {
    const cls = e.source === 'builtin' ? 'calendar-chip-builtin' : 'calendar-chip-custom';
    return `<div class="calendar-chip ${cls}" data-date="${date}" data-tooltip="${escapeAttr(e.text)}" role="button">${escapeHtml(e.text)}</div>`;
  }).join('');
  const moreChip = more > 0
    ? `<div class="calendar-more" data-date="${date}">+${more} 更多</div>`
    : '';
  return `
    <div class="calendar-cell ${isToday ? 'today' : ''}">
      <span class="calendar-date">${date}</span>
      <div class="calendar-events">
        ${chips}
        ${moreChip}
      </div>
    </div>
  `;
}

async function renderCalendar() {
  const grid = document.getElementById('calendar-grid');
  const titleEl = document.getElementById('cal-title');

  // 首次进入日历视图：初始化为浏览器当前年月
  if (state.calendar.year === 0) {
    const now = getNowInBrowser();
    state.calendar.year = now.year;
    state.calendar.month = now.month;
  }

  const { year, month } = state.calendar;
  titleEl.textContent = formatMonthTitle(year, month);

  try {
    const resp = await api.getMonth(year, month);
    if (!resp.success) throw new Error(resp.error || '获取月历失败');

    // 按 day 聚合（builtin + custom 合并）
    const byDay = {};
    [...resp.builtin, ...resp.custom].forEach(e => {
      const d = e.day;
      if (!byDay[d]) byDay[d] = [];
      byDay[d].push(e);
    });
    // 同日内 builtin 在前、custom 在后（颜色对比更清晰）
    Object.values(byDay).forEach(arr => {
      arr.sort((a, b) => {
        const sa = a.source === 'builtin' ? 0 : 1;
        const sb = b.source === 'builtin' ? 0 : 1;
        return sa - sb;
      });
    });

    const offset = firstDayOffset(year, month);
    const total = daysInMonth(year, month);
    const cells = [];
    // 前置空格
    for (let i = 0; i < offset; i++) cells.push(renderCalendarCell(null, []));
    // 当月每天
    for (let d = 1; d <= total; d++) {
      cells.push(renderCalendarCell(d, byDay[d] || []));
    }
    // 后置空格凑满 6 行（42 格）
    while (cells.length < 42) cells.push(renderCalendarCell(null, []));

    grid.innerHTML = renderWeekdayHeader() + cells.join('');

    // 绑定 "+N 更多" 点击
    grid.querySelectorAll('.calendar-more').forEach(el => {
      el.addEventListener('click', () => {
        const day = parseInt(el.dataset.date, 10);
        showDayModal(year, month, day, byDay[day] || []);
      });
    });
    // chip：仅在文本被 ellipsis 截断时启用 tooltip
    // 完整显示的 chip 完全不交互（无 tooltip、无 cursor 变化）
    grid.querySelectorAll('.calendar-chip').forEach(el => {
      if (el.scrollWidth <= el.clientWidth) return; // 未截断，跳过
      el.classList.add('truncated');
      el.addEventListener('mouseenter', () => showChipTooltip(el));
      el.addEventListener('mousemove', positionChipTooltip);
      el.addEventListener('mouseleave', hideChipTooltip);
    });
  } catch (e) {
    grid.innerHTML = `<div class="empty-state" style="grid-column: 1 / -1;">⚠️ ${escapeHtml(e.message)}</div>`;
  }
}

function changeMonth(delta) {
  let { year, month } = state.calendar;
  month += delta;
  if (month < 1) { month = 12; year--; }
  if (month > 12) { month = 1; year++; }
  state.calendar.year = year;
  state.calendar.month = month;
  renderCalendar();
}

function jumpToToday() {
  const now = getNowInBrowser();
  state.calendar.year = now.year;
  state.calendar.month = now.month;
  renderCalendar();
}

function showDayModal(year, month, day, events) {
  document.getElementById('day-modal-title').textContent = `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
  const body = events.length === 0
    ? '<div class="empty-state">当日无事件</div>'
    : events.map(e => {
        const tag = e.source === 'builtin'
          ? '<span class="badge">内置</span>'
          : '<span class="badge badge-primary">自定义</span>';
        const repeat = (e.repeat !== undefined && e.repeat !== 0)
          ? ` <span class="badge badge-warning">重复 ${e.repeat}</span>` : '';
        return `<div style="margin-bottom: 8px;">${tag}${repeat} <strong>${escapeHtml(e.text)}</strong></div>`;
      }).join('');
  document.getElementById('day-modal-body').innerHTML = body;
  document.getElementById('day-modal').classList.add('active');
}

// ==================== [6] render: tasks ====================
async function renderTasks() {
  const wrap = document.getElementById('tasks-content');
  try {
    const resp = await api.getTasks();
    if (!resp.success) throw new Error(resp.error || '获取任务失败');
    state.tasks = resp.tasks;

    if (state.tasks.length === 0) {
      wrap.innerHTML = `<div class="empty-state">暂无待触发任务</div>`;
      return;
    }

    const rows = state.tasks.map(t => {
      const kindBadge = t.kind === 'calendar'
        ? '<span class="badge badge-primary">日历</span>'
        : '<span class="badge badge-warning">后续</span>';
      const targetInfo = t.target_user_id
        ? ` → <code style="font-family: var(--mono); color: var(--text-muted);">${escapeHtml(t.target_user_id)}</code>`
        : '';
      const hintDisplay = t.hint ? escapeHtml(t.hint) : '<span style="color: var(--text-muted);">—</span>';
      return `
        <tr>
          <td>${kindBadge}</td>
          <td class="col-time">${escapeHtml(formatIsoLocal(t.fire_at_iso))}</td>
          <td><code style="font-family: var(--mono); font-size: 12px; color: var(--text-muted);">${escapeHtml(t.session)}</code></td>
          <td>${hintDisplay}${targetInfo}</td>
          <td class="col-actions">
            <button class="btn btn-danger btn-sm" data-cancel-id="${escapeAttr(t.id)}">取消</button>
          </td>
        </tr>
      `;
    }).join('');

    wrap.innerHTML = `
      <div class="table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>类型</th>
              <th>触发时间</th>
              <th>会话</th>
              <th>内容</th>
              <th class="col-actions">操作</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    `;

    wrap.querySelectorAll('[data-cancel-id]').forEach(btn => {
      btn.addEventListener('click', () => {
        const taskId = btn.dataset.cancelId;
        const task = state.tasks.find(t => t.id === taskId);
        if (!task) return;
        askCancelTask(task);
      });
    });
  } catch (e) {
    wrap.innerHTML = `<div class="empty-state">⚠️ ${escapeHtml(e.message)}</div>`;
  }
}

function askCancelTask(task) {
  const detail = task.kind === 'calendar'
    ? `日历提醒（${formatIsoLocal(task.fire_at_iso)}）`
    : `后续任务（${formatIsoLocal(task.fire_at_iso)}）：${task.hint || '—'}`;
  showConfirm('取消此任务？', detail, async () => {
    try {
      const resp = await api.cancelTask(task.id);
      if (!resp.success) {
        toast('error', resp.error || '取消失败');
        return;
      }
      toast('success', '任务已取消');
      renderTasks();
      // 同步刷新 dashboard 计数（如果用户切回去）
      renderDashboard().catch(() => {});
    } catch (e) {
      toast('error', `取消失败: ${e.message}`);
    }
  });
}

// ==================== [7] modal / toast / tooltip 辅助 ====================

/** 自定义 hover tooltip：跟随鼠标位置，无浏览器原生 1-2s 延迟 */
let _chipTooltipEl = null;

function ensureChipTooltip() {
  if (_chipTooltipEl) return _chipTooltipEl;
  _chipTooltipEl = document.createElement('div');
  _chipTooltipEl.id = 'chip-tooltip';
  _chipTooltipEl.className = 'chip-tooltip';
  _chipTooltipEl.style.display = 'none';
  document.body.appendChild(_chipTooltipEl);
  return _chipTooltipEl;
}

function showChipTooltip(el) {
  const tip = ensureChipTooltip();
  tip.textContent = el.dataset.tooltip || '';
  tip.style.display = 'block';
}

function positionChipTooltip(ev) {
  const tip = _chipTooltipEl;
  if (!tip || tip.style.display === 'none') return;
  const padding = 12;
  // 默认显示在鼠标右上方；右边/上边空间不够则翻到左/下
  const tipRect = tip.getBoundingClientRect();
  let x = ev.clientX + padding;
  let y = ev.clientY - tipRect.height - padding;
  if (x + tipRect.width > window.innerWidth) x = ev.clientX - tipRect.width - padding;
  if (y < 0) y = ev.clientY + padding;
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
}

function hideChipTooltip() {
  if (_chipTooltipEl) _chipTooltipEl.style.display = 'none';
}

function showConfirm(title, body, onOk) {
  document.getElementById('confirm-title').textContent = title;
  document.getElementById('confirm-body').innerHTML = body;
  const overlay = document.getElementById('confirm-modal');
  overlay.classList.add('active');

  const okBtn = document.getElementById('confirm-ok');
  const cancelBtn = document.getElementById('confirm-cancel');

  const cleanup = () => {
    overlay.classList.remove('active');
    okBtn.onclick = null;
    cancelBtn.onclick = null;
  };
  okBtn.onclick = () => { cleanup(); onOk && onOk(); };
  cancelBtn.onclick = cleanup;
}

function toast(type, message) {
  const wrap = document.getElementById('toast-wrap');
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = message;
  wrap.appendChild(el);
  setTimeout(() => {
    el.style.opacity = '0';
    el.style.transition = 'opacity 0.3s';
    setTimeout(() => el.remove(), 300);
  }, 3000);
}

// ==================== 工具函数 ====================
function escapeHtml(str) {
  if (str === undefined || str === null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function escapeAttr(str) {
  return escapeHtml(str);
}

/** 把 ISO 字符串原样展示为 YYYY-MM-DD HH:MM（避免浏览器时区转换） */
function formatIsoLocal(iso) {
  if (!iso) return '—';
  // 截取 "YYYY-MM-DDTHH:MM" 部分，把 T 换成空格
  const m = String(iso).match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/);
  if (!m) return iso;
  return `${m[1]} ${m[2]}`;
}

// ==================== [9] init ====================
function bindNav() {
  document.querySelectorAll('.nav-item').forEach(btn => {
    btn.addEventListener('click', () => switchView(btn.dataset.view));
  });
  document.getElementById('cal-prev').addEventListener('click', () => changeMonth(-1));
  document.getElementById('cal-next').addEventListener('click', () => changeMonth(1));
  document.getElementById('cal-today').addEventListener('click', jumpToToday);
  document.getElementById('refresh-btn').addEventListener('click', refreshCurrentView);
  document.getElementById('theme-toggle').addEventListener('click', toggleTheme);
  document.getElementById('day-modal-close').addEventListener('click', () => {
    document.getElementById('day-modal').classList.remove('active');
  });
  document.getElementById('day-modal').addEventListener('click', (e) => {
    if (e.target.id === 'day-modal') {
      document.getElementById('day-modal').classList.remove('active');
    }
  });
  document.getElementById('confirm-modal').addEventListener('click', (e) => {
    if (e.target.id === 'confirm-modal') {
      document.getElementById('confirm-modal').classList.remove('active');
    }
  });
}

function toggleTheme() {
  const html = document.documentElement;
  const current = html.getAttribute('data-theme') || 'light';
  const next = current === 'light' ? 'dark' : 'light';
  html.setAttribute('data-theme', next);
  try { localStorage.setItem('astrbot-theme', next); } catch (e) {}
}

async function loadAbout() {
  try {
    const resp = await api.getAbout();
    if (resp.success) {
      state.about = resp;
      document.getElementById('version-tag').textContent = resp.version || '1.0.0';
      if (resp.display_name) {
        document.getElementById('plugin-name-tag').textContent = resp.name || PLUGIN_NAME;
      }
    }
  } catch (e) { /* 静默：版本号非关键 */ }
}

async function init() {
  bindNav();
  await waitBridgeReady();
  loadAbout();
  switchView('dashboard');
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
