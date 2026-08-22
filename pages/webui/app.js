/**
 * time_awareness WebUI
 *
 * 5 个 view：概览 / 日历（月视图）/ 任务 / 静态日程 / 人格日程
 * vanilla JS，无框架，无 URL router（DOM 显隐切换）
 */

// ==================== [1] 桥接 + 全局状态 ====================
const PLUGIN_NAME = 'time_awareness';

// 兼容 AstrBot 主 webui 注入的 bridge；缺失时降级 fetch（用相对路径）
const bridge = window.AstrBotPluginPage || null;

const state = {
  currentView: 'dashboard',
  calendar: { year: 0, month: 0, token: 0 }, // token 防止快速翻月时旧响应覆盖新月份
  stats: null,
  tasks: [],
  about: null,
  taskOptions: null, // { sessions, now_iso, tz_label, default_fire_at_local }
  staticSchedules: {
    loaded: false,
    revision: '',
    slots: [],
    warnings: [],
    maxSlots: 256,
    page: 0,
    pageSize: 20,
    dirty: false,
    saving: false,
    editingIndex: null,
  },
  schedules: {
    personas: [],   // [{ key, name, snapshot_count, dates, latest_date }]
    personasLoaded: false,
    detail: null,   // 当前选中的快照详情
    draft: null,    // 编辑草稿 { snapshot_id, slots: [...] }
    dirty: false,
    detailToken: 0, // 详情请求序号：过期响应不得覆盖当前选择
    saving: false,  // 保存请求进行中（防重复提交）
  },
};

const VIEW_TITLES = {
  dashboard: '概览',
  calendar: '日历',
  tasks: '任务',
  'static-schedules': '静态日程',
  schedules: '人格日程',
};

// ==================== [2] API client ====================
// 桥接模式下 endpoint 不带插件名前缀——父窗口自动拼 /{plugin_name}/<endpoint>
// 降级模式（未注入 bridge）按 AstrBot 4.26.3 路由拼 /api/plug/{plugin_name}/<endpoint>
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
  const url = `/api/plug/${PLUGIN_NAME}/${endpoint}${query ? '?' + query : ''}`;
  const resp = await fetch(url);
  return resp.json();
}

async function apiPost(endpoint, body = {}) {
  if (bridge && typeof bridge.apiPost === 'function') {
    return bridge.apiPost(endpoint, body);
  }
  const resp = await fetch(`/api/plug/${PLUGIN_NAME}/${endpoint}`, {
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
  getTaskOptions: () => apiGet('tasks/options'),
  createTask: (payload) => apiPost('tasks/create', payload),
  getSchedulePersonas: () => apiGet('schedules/personas'),
  getScheduleDetail: (persona_hash, date, timezone) =>
    apiGet('schedules/detail', { persona_hash, date, timezone }),
  saveSchedule: (payload) => apiPost('schedules/save', payload),
  getStaticSchedules: () => apiGet('static-schedules'),
  saveStaticSchedules: (payload) => apiPost('static-schedules/save', payload),
};

/** 等待桥接 SDK 与父窗口握手完成。降级模式立即返回。 */
async function waitBridgeReady() {
  if (bridge && typeof bridge.ready === 'function') {
    try { await bridge.ready(); } catch (e) { /* 握手失败仍允许尝试调用 */ }
  }
}

// ==================== [3] view switcher ====================
function applyView(name) {
  state.currentView = name;
  // 离开 dashboard 时停止时钟（避免后台空跑 setInterval）
  if (state.currentView !== 'dashboard') stopHeroClock();
  document.querySelectorAll('.nav-item').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.view === name);
  });
  document.querySelectorAll('.view').forEach(v => {
    v.classList.toggle('active', v.dataset.view === name);
  });
  document.getElementById('view-title').textContent = VIEW_TITLES[name] || name;
  if (name !== 'schedules') stopScheduleClock();
  // 日程操作常驻页眉，滚动时不消失。
  const addTop = document.getElementById('sch-add-slot-top');
  const saveTop = document.getElementById('sch-save-top');
  if (addTop) addTop.style.display = name === 'schedules' ? '' : 'none';
  if (saveTop) saveTop.style.display = name === 'schedules' ? '' : 'none';
  refreshCurrentView();
}

function switchView(name) {
  const leavingPersonaDraft = state.currentView === 'schedules'
    && name !== 'schedules'
    && state.schedules.dirty;
  const leavingStaticDraft = state.currentView === 'static-schedules'
    && name !== 'static-schedules'
    && state.staticSchedules.dirty;
  if (leavingPersonaDraft || leavingStaticDraft) {
    const currentDraft = leavingStaticDraft ? state.staticSchedules : state.schedules;
    const draftName = leavingStaticDraft ? '静态日程' : '人格日程';
    showConfirm(
      '放弃未保存的修改？',
      `当前${draftName}草稿尚未保存，离开页面将丢失这些修改。`,
      () => {
        currentDraft.dirty = false;
        applyView(name);
      },
    );
    return;
  }
  if (name === state.currentView && name === 'schedules' && state.schedules.dirty) return;
  applyView(name);
}

async function refreshCurrentView() {
  switch (state.currentView) {
    case 'dashboard': return renderDashboard();
    case 'calendar': return renderCalendar();
    case 'tasks': return renderTasks();
    case 'static-schedules': return renderStaticSchedules();
    case 'schedules': return renderSchedules();
  }
}

// ==================== [4] render: dashboard ====================

// 工作日性质 → chip 配色类名映射
const WORKDAY_CHIP_CLASS = {
  workday: 'workday-chip-workday',
  weekend: 'workday-chip-weekend',
  adjusted: 'workday-chip-adjusted',
  holiday: 'workday-chip-holiday',
  unknown: 'workday-chip-unknown',
};

async function renderDashboard() {
  const wrap = document.getElementById('dashboard-content');
  try {
    const resp = await api.getStats();
    if (!resp.success) throw new Error(resp.error || '获取统计失败');
    state.stats = resp.stats;

    const s = state.stats;

    // ---- Hero card：时间感知面板 ----
    // GMT 时区尾巴（贴在主时间后下对齐；未配时区时为空）
    const tzGmtHtml = s.tz_gmt
      ? `<span class="hero-time-tz">${escapeHtml(s.tz_gmt)}</span>`
      : '';
    const chipClass = WORKDAY_CHIP_CLASS[s.workday_kind] || 'workday-chip-unknown';
    // 工作日 chip 短文本：从 "工作日（周五）" 拆出 "工作日"
    // weekday_display 已单独提供"周五"，chip 内只保留性质短语避免重复
    const workdayShort = (s.workday_label || '').split('（')[0].trim() || (s.workday_label || '');
    // 工作日 chip：开关关闭（workday_label 空）时不渲染
    const workdayChipHtml = s.workday_label
      ? `<span class="workday-chip ${chipClass}">${escapeHtml(workdayShort)}</span>`
      : '';
    // 农历年生肖 chip：未启用或异常时不渲染
    const lunarYearChipHtml = s.lunar_year_chip
      ? `<span class="lunar-year-chip">${escapeHtml(s.lunar_year_chip)}</span>`
      : '';
    // 农历月日：紧贴在 chip 后，前面不加逗号
    const lunarMdHtml = s.lunar_month_day
      ? `<span class="date-info-seg">${escapeHtml(s.lunar_month_day)}</span>`
      : '';
    // 周X：始终显示（除非后端字段缺失）
    const weekdayHtml = s.weekday_display
      ? `<span class="date-info-seg">${escapeHtml(s.weekday_display)}</span>`
      : '';
    // 黄历 chip 行：紧贴 date-info 下方（与农历同语义块）
    // 拆 3 个 chip：📜干支冲煞（紫）/ 宜（绿）/ 忌（红）
    // 任一字段为空则跳过该 chip；全部为空时不渲染整行
    const almanacChips = [];
    if (s.almanac_gz_day) {
      const chong = s.almanac_chong_sha ? ` ${escapeHtml(s.almanac_chong_sha)}` : '';
      almanacChips.push(
        `<span class="almanac-chip almanac-chip-gz">📜 ${escapeHtml(s.almanac_gz_day)}${chong}</span>`
      );
    }
    if (s.almanac_yi) {
      almanacChips.push(
        `<span class="almanac-chip almanac-chip-yi">宜 ${escapeHtml(s.almanac_yi)}</span>`
      );
    }
    if (s.almanac_ji) {
      almanacChips.push(
        `<span class="almanac-chip almanac-chip-ji">忌 ${escapeHtml(s.almanac_ji)}</span>`
      );
    }
    const almanacHtml = almanacChips.length
      ? `<div class="dashboard-almanac-row">${almanacChips.join('')}</div>`
      : '';

    const heroHtml = `
      <div class="dashboard-hero card">
        <div class="hero-time">
          <span class="hero-time-main">${escapeHtml(s.time_display || '--:--:--')}</span>${tzGmtHtml}
        </div>
        <div class="greeting">${escapeHtml(s.greeting || '')}</div>
        <div class="date-info">
          <span class="date-info-prefix">今天是</span>
          <span class="date-info-seg">${escapeHtml(s.date_display || '')}</span>
          ${lunarYearChipHtml}
          ${lunarMdHtml}
          ${weekdayHtml}
          ${workdayChipHtml}
        </div>
        ${almanacHtml}
      </div>
    `;

    // ---- 原 metrics-grid（统计卡片） ----
    const metricsHtml = `
      <div class="metrics-grid">
        <div class="metric-card">
          <div class="metric-label">自定义事件总数</div>
          <div class="metric-value">${escapeHtml(String(s.custom_event_total ?? 0))}</div>
          <div class="metric-sub">calendar_data.yaml</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">内置事件总数</div>
          <div class="metric-value">${escapeHtml(String(s.builtin_event_total ?? 0))}</div>
          <div class="metric-sub">节日 / 节气</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">本月事件</div>
          <div class="metric-value">${escapeHtml(String(s.this_month_event_count ?? 0))}</div>
          <div class="metric-sub">含 builtin + custom</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">待触发任务</div>
          <div class="metric-value">${escapeHtml(String(s.task_pending_total ?? 0))}</div>
          <div class="metric-sub">日历 ${escapeHtml(String(s.task_calendar_pending ?? 0))} · 后续 ${escapeHtml(String(s.task_followup_pending ?? 0))} · 手动 ${escapeHtml(String(s.task_user_pending || 0))}${s.task_retry_wait ? ' · 重试 ' + escapeHtml(String(s.task_retry_wait)) : ''}${s.task_failed_total ? ' · 失败 ' + escapeHtml(String(s.task_failed_total)) : ''}</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">近 7 天任务</div>
          <div class="metric-value">${escapeHtml(String(s.near_7d_task_count ?? 0))}</div>
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

    wrap.innerHTML = heroHtml + metricsHtml;
    // 启动 hero 时钟（每秒 tick 一次 time_display 字段，其他字段保持渲染时刻的值）。
    // 非 dashboard 视图（如取消/建任务后刷新计数）不启动后台空转定时器。
    if (state.currentView === 'dashboard') {
      startHeroClock(s.time_display);
    } else {
      stopHeroClock();
    }
  } catch (e) {
    stopHeroClock();
    wrap.innerHTML = `<div class="empty-state">⚠️ ${escapeHtml(e.message)}</div>`;
  }
}

// ==================== [4b] hero 时钟（轻量 setInterval，仅更新时间显示） ====================
let _heroClockHandle = null;

function stopHeroClock() {
  if (_heroClockHandle) {
    clearInterval(_heroClockHandle);
    _heroClockHandle = null;
  }
}

/**
 * 从服务端 time_display（HH:MM:SS）起步，每秒 +1 推进时间。
 * 不依赖浏览器时区——直接拿服务端给的字符串做整数累加，
 * 这样插件配置时区与显示时区始终一致。
 * DOM 不存在（用户切走 view）时自动停止。
 */
function startHeroClock(timeDisplay) {
  stopHeroClock();
  const m = String(timeDisplay || '').match(/^(\d{2}):(\d{2}):(\d{2})$/);
  if (!m) return;
  let h = parseInt(m[1], 10);
  let mi = parseInt(m[2], 10);
  let s = parseInt(m[3], 10);
  if (h > 23 || mi > 59 || s > 59) return;
  const fmt = (n) => String(n).padStart(2, '0');
  const tick = () => {
    s += 1;
    if (s >= 60) { s = 0; mi += 1; }
    if (mi >= 60) { mi = 0; h += 1; }
    if (h >= 24) { h = 0; }
    const el = document.querySelector('.hero-time-main');
    if (!el) { stopHeroClock(); return; }
    el.textContent = `${fmt(h)}:${fmt(mi)}:${fmt(s)}`;
  };
  _heroClockHandle = setInterval(tick, 1000);
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
  const requestToken = ++state.calendar.token;
  titleEl.textContent = formatMonthTitle(year, month);

  try {
    const resp = await api.getMonth(year, month);
    if (
      requestToken !== state.calendar.token ||
      year !== state.calendar.year ||
      month !== state.calendar.month
    ) return;
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
    if (requestToken !== state.calendar.token) return;
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
          ? ` <span class="badge badge-warning">重复 ${escapeHtml(String(e.repeat))}</span>` : '';
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
    const finished = Array.isArray(resp.finished) ? resp.finished : [];

    if (state.tasks.length === 0 && finished.length === 0) {
      wrap.innerHTML = `<div class="empty-state">暂无任务</div>`;
      return;
    }

    const rows = state.tasks.map(t => {
      let kindBadge;
      if (t.kind === 'calendar') {
        kindBadge = '<span class="badge badge-primary">日历</span>';
      } else if (t.kind === 'user') {
        kindBadge = '<span class="badge badge-success">手动</span>';
      } else {
        kindBadge = '<span class="badge badge-warning">后续</span>';
      }
      const targetInfo = t.target_user_id
        ? ` → <code style="font-family: var(--mono); color: var(--text-muted);">${escapeHtml(t.target_user_id)}</code>`
        : '';
      const hintDisplay = t.hint ? escapeHtml(t.hint) : '<span style="color: var(--text-muted);">—</span>';
      const retryBadge = t.status === 'retry_wait'
        ? `<span class="badge badge-warning" title="已尝试 ${Number(t.attempts || 0)} 次">等待重试</span>`
        : '';
      const fireDisplay = t.status === 'retry_wait' && t.next_retry_at_iso
        ? `${escapeHtml(formatIsoLocal(t.next_retry_at_iso))}<div style="font-size:11px;color:var(--text-muted);">原定 ${escapeHtml(formatIsoLocal(t.fire_at_iso))}</div>`
        : escapeHtml(formatIsoLocal(t.fire_at_iso));
      return `
        <tr>
          <td data-label="类型">${kindBadge} ${retryBadge}</td>
          <td data-label="触发时间" class="col-time">${fireDisplay}</td>
          <td data-label="会话"><code style="font-family: var(--mono); font-size: 12px; color: var(--text-muted); word-break: break-all;">${escapeHtml(t.session)}</code></td>
          <td data-label="内容">${hintDisplay}${targetInfo}</td>
          <td data-label="操作" class="col-actions">
            <button class="btn btn-danger btn-sm" data-cancel-id="${escapeAttr(t.id)}">取消</button>
          </td>
        </tr>
      `;
    }).join('');

    const statusMeta = {
      sent: { cls: 'badge-success', label: '已发送' },
      failed: { cls: 'badge-danger', label: '失败' },
      cancelled: { cls: 'badge-muted', label: '已取消' },
      expired: { cls: 'badge-warning', label: '已过期' },
    };

    const finishedRows = finished.map(t => {
      let kindBadge;
      if (t.kind === 'calendar') {
        kindBadge = '<span class="badge badge-primary">日历</span>';
      } else if (t.kind === 'user') {
        kindBadge = '<span class="badge badge-success">手动</span>';
      } else {
        kindBadge = '<span class="badge badge-warning">后续</span>';
      }
      const meta = statusMeta[t.status] || { cls: 'badge-muted', label: escapeHtml(t.status) };
      const hintDisplay = t.hint ? escapeHtml(t.hint) : '<span style="color: var(--text-muted);">—</span>';
      const errorDisplay = t.last_error
        ? `<div style="font-size:11px;color:var(--danger, #e5484d);" title="${escapeAttr(t.last_error)}">${escapeHtml(String(t.last_error).slice(0, 60))}</div>`
        : '';
      return `
        <tr class="row-finished">
          <td data-label="类型">${kindBadge} <span class="badge ${meta.cls}">${meta.label}</span></td>
          <td data-label="触发时间" class="col-time">${escapeHtml(formatIsoLocal(t.fire_at_iso))}</td>
          <td data-label="结束时间" class="col-time">${escapeHtml(formatIsoLocal(t.terminal_at_iso))}</td>
          <td data-label="会话"><code style="font-family: var(--mono); font-size: 12px; color: var(--text-muted); word-break: break-all;">${escapeHtml(t.session)}</code></td>
          <td data-label="内容">${hintDisplay}${errorDisplay}</td>
        </tr>
      `;
    }).join('');

    const finishedSection = finished.length
      ? `
        <h3 class="tasks-subtitle">已结束（保留 ${escapeHtml(String(resp.retention_days ?? 7))} 天内）</h3>
        <div class="table-wrap">
          <table class="data-table">
            <thead>
              <tr>
                <th>类型</th>
                <th>触发时间</th>
                <th>结束时间</th>
                <th>会话</th>
                <th>内容</th>
              </tr>
            </thead>
            <tbody>${finishedRows}</tbody>
          </table>
        </div>
      `
      : '';

    wrap.innerHTML = `
      ${state.tasks.length
        ? `<div class="table-wrap">
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
          </div>`
        : `<div class="empty-state">暂无待触发任务</div>`}
      ${finishedSection}
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
  let detail;
  const hint = escapeHtml(task.hint || '—');
  if (task.kind === 'calendar') {
    detail = `日历提醒（${formatIsoLocal(task.fire_at_iso)}）`;
  } else if (task.kind === 'user') {
    detail = `手动提醒（${formatIsoLocal(task.fire_at_iso)}）：${hint}`;
  } else {
    detail = `后续任务（${formatIsoLocal(task.fire_at_iso)}）：${hint}`;
  }
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

// ==================== [6b] 新增任务 modal ====================

async function openCreateTaskModal() {
  // 拉取 options（白名单为空则提前提示）
  let opts;
  try {
    const resp = await api.getTaskOptions();
    if (!resp.success) throw new Error(resp.error || '获取选项失败');
    opts = resp;
  } catch (e) {
    toast('error', `无法加载选项：${e.message}`);
    return;
  }
  state.taskOptions = opts;

  if (!opts.sessions || opts.sessions.length === 0) {
    toast('info', 'reminder_targets 为空，请先在配置里添加白名单会话');
    return;
  }

  // 填充表单
  const sel = document.getElementById('tf-session');
  sel.innerHTML = opts.sessions
    .map(s => `<option value="${escapeAttr(s)}">${escapeHtml(s)}</option>`)
    .join('');
  document.getElementById('tf-fire').value = opts.default_fire_at_local;
  document.getElementById('tf-tz-label').textContent = `(时区: ${opts.tz_label})`;
  document.getElementById('tf-hint').value = '';
  document.getElementById('tf-target').value = '';

  document.getElementById('task-create-modal').classList.add('active');
}

async function submitCreateTask() {
  const session = document.getElementById('tf-session').value;
  const fire_at_local = document.getElementById('tf-fire').value;
  const hint = document.getElementById('tf-hint').value.trim();
  const target_user_id = document.getElementById('tf-target').value.trim();

  // 体验性预检（非硬约束，后端会再校验）
  if (!fire_at_local) { toast('error', '请填写触发时间'); return; }
  if (!hint) { toast('error', '请填写提醒内容'); return; }

  // 实时预检：默认时间由后端按插件时区生成，浏览器按本地时区解释
  // 「无时区字符串」会产生固定偏移（浏览器时区 ≠ 插件时区时），用
  // server_now_ms 与默认值之差算出该偏移并校正，避免时区偏东的浏览器
  // 一律误拒。校正后与浏览器真实时钟比较，停留过久也不依赖旧快照。
  // 余量 90s 比后端 60s 严格，确保前端先拦下
  const opts = state.taskOptions || {};
  let browserOffset = 0;
  if (opts.server_now_ms && opts.default_fire_at_local) {
    browserOffset = Date.parse(opts.default_fire_at_local) -
      (Number(opts.server_now_ms) + 10 * 60 * 1000);
  }
  const fireMs = Date.parse(fire_at_local);
  const realNowMs = Date.now();
  if (!isNaN(fireMs) && fireMs - browserOffset - realNowMs < 90000) {
    toast(
      'error',
      '触发时间距当前过近（弹窗默认时间是打开时算的 +10 分钟，停留过久会被追上）。' +
      `当前浏览器时间 ${new Date(realNowMs).toLocaleTimeString()}，请重新选一个至少 1 分钟后的时刻`
    );
    return;
  }

  const submitBtn = document.getElementById('tf-submit');
  submitBtn.disabled = true;
  try {
    const resp = await api.createTask({ session, fire_at_local, hint, target_user_id });
    if (!resp.success) {
      toast('error', resp.error || '创建失败');
      return;
    }
    document.getElementById('task-create-modal').classList.remove('active');
    toast('success', '任务已创建');
    renderTasks();
    renderDashboard().catch(() => {});
  } catch (e) {
    toast('error', `创建失败：${e.message}`);
  } finally {
    submitBtn.disabled = false;
  }
}

function closeCreateTaskModal() {
  document.getElementById('task-create-modal').classList.remove('active');
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
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  // 默认显示在鼠标右上方；右边/上边空间不够则翻到左/下
  const tipRect = tip.getBoundingClientRect();
  let x = ev.clientX + padding;
  let y = ev.clientY - tipRect.height - padding;
  if (x + tipRect.width > vw) x = ev.clientX - tipRect.width - padding;
  if (y < 0) y = ev.clientY + padding;
  // 翻转后仍可能溢出（chip 极度靠左/右/下）—— clamp 到屏幕内
  if (x < padding) x = padding;
  if (x + tipRect.width > vw - padding) x = Math.max(padding, vw - tipRect.width - padding);
  if (y < padding) y = padding;
  if (y + tipRect.height > vh - padding) y = Math.max(padding, vh - tipRect.height - padding);
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
}

function hideChipTooltip() {
  if (_chipTooltipEl) _chipTooltipEl.style.display = 'none';
}

function showConfirm(title, body, onOk, onCancel) {
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
  cancelBtn.onclick = () => { cleanup(); onCancel && onCancel(); };
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

// ==================== [8b] 人格日程 ====================

const SLOT_MODE_LABEL = {
  finished: '已结束 · 只读',
  active: '正在进行 · 仅结束时间可编辑',
  imminent: '即将开始 · 仅结束时间可改',
  future: '未来 · 可编辑',
  new: '新增 · 草稿',
};

// ==================== 时区安全工具 ====================
// 快照后端存储使用 IANA key（如 Asia/Shanghai）或固定偏移回退键（UTC+08:00）
// 或 system-local。所有 Intl.DateTimeFormat 调用必须先经 safeTimeZone 验证：
// 固定偏移/无效时区不能直接当 timeZone 传给 Intl（会抛 RangeError 使按钮失效）。
// 计算基于 IANA；面向用户统一显示为 UTC+x（必要时带分钟）。

function safeTimeZone(tz) {
  // 只接受明确的 IANA 区域格式（如 Asia/Shanghai、Etc/UTC）或 UTC/GMT；
  // 拒绝 CST 之类歧义缩写（不同 ICU 实现可能把 CST 解析成不同含义）以及
  // UTC+08:00 固定偏移。Intl.DateTimeFormat 构造器作二次校验，无效返回 undefined。
  if (typeof tz !== 'string') return undefined;
  const name = tz.trim();
  if (!name) return undefined;
  const looksIana = name === 'UTC' || name === 'GMT' || name.indexOf('/') !== -1;
  if (!looksIana) return undefined;
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: name });
    return name;
  } catch (e) {
    return undefined;
  }
}

function fixedOffsetMinutes(tz) {
  // 解析固定偏移 UTC+08:00 / UTC+5:30 / GMT-3 等 → 分钟数；非固定偏移返回 null
  if (typeof tz !== 'string') return null;
  const m = tz.trim().match(/^(?:UTC|GMT)([+-])(\d{1,2})(?::(\d{2}))?$/i);
  if (!m) return null;
  const sign = m[1] === '-' ? -1 : 1;
  const hours = parseInt(m[2], 10);
  const minutes = parseInt(m[3] || '0', 10);
  if (hours > 14 || minutes > 59) return null;
  return sign * (hours * 60 + minutes);
}

function currentOffsetMinutes(tz) {
  // 计算 IANA 时区当前偏移（含夏令时），用于面向用户显示 UTC+x；失败返回 null
  const name = safeTimeZone(tz);
  if (!name) return null;
  try {
    const now = new Date();
    const dtf = new Intl.DateTimeFormat('en-US', {
      timeZone: name,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hourCycle: 'h23',
    });
    const parts = {};
    for (const p of dtf.formatToParts(now)) parts[p.type] = p.value;
    const asUTC = Date.UTC(
      Number(parts.year), Number(parts.month) - 1, Number(parts.day),
      Number(parts.hour), Number(parts.minute), Number(parts.second)
    );
    return Math.round((asUTC - now.getTime()) / 60000);
  } catch (e) {
    return null;
  }
}

function formatOffsetLabel(offsetMinutes) {
  const sign = offsetMinutes < 0 ? '-' : '+';
  const abs = Math.abs(offsetMinutes);
  const hh = Math.floor(abs / 60);
  const mm = abs % 60;
  return mm === 0 ? `UTC${sign}${hh}` : `UTC${sign}${hh}:${String(mm).padStart(2, '0')}`;
}

function timezoneDisplayLabel(tz) {
  // 面向用户显示的时区标签：IANA → UTC+x；固定偏移 → UTC+x；system-local → 系统本地
  if (!tz) return '';
  const name = String(tz).trim();
  if (name === 'system-local') return '系统本地';
  const off = fixedOffsetMinutes(name);
  if (off !== null && Number.isFinite(off)) return formatOffsetLabel(off);
  const ianaOff = currentOffsetMinutes(name);
  if (ianaOff !== null && Number.isFinite(ianaOff)) return formatOffsetLabel(ianaOff);
  return name;
}

function formatInTimeZone(date, tz, locale, options) {
  // 安全格式化：IANA → Intl；固定偏移 → 平移后按 UTC 格式化（等价本地墙钟）；
  // system-local/无效 → 浏览器本地。永不因无效时区抛 RangeError。
  const opts = options || {};
  const iana = safeTimeZone(tz);
  if (iana) {
    return new Intl.DateTimeFormat(locale, Object.assign({}, opts, { timeZone: iana })).format(date);
  }
  const off = fixedOffsetMinutes(tz);
  if (off !== null && Number.isFinite(off)) {
    const shifted = new Date(date.getTime() + off * 60000);
    return new Intl.DateTimeFormat(locale, Object.assign({}, opts, { timeZone: 'UTC' })).format(shifted);
  }
  return new Intl.DateTimeFormat(locale, opts).format(date);
}

function formatToPartsInTimeZone(date, tz, locale, options) {
  const opts = options || {};
  const iana = safeTimeZone(tz);
  if (iana) {
    return new Intl.DateTimeFormat(locale, Object.assign({}, opts, { timeZone: iana })).formatToParts(date);
  }
  const off = fixedOffsetMinutes(tz);
  if (off !== null && Number.isFinite(off)) {
    const shifted = new Date(date.getTime() + off * 60000);
    return new Intl.DateTimeFormat(locale, Object.assign({}, opts, { timeZone: 'UTC' })).formatToParts(shifted);
  }
  return new Intl.DateTimeFormat(locale, opts).formatToParts(date);
}

let scheduleClockTimer = null;
let scheduleClockBaseMs = 0;
let scheduleClockReceivedMs = 0;

function stopScheduleClock() {
  if (scheduleClockTimer) clearInterval(scheduleClockTimer);
  scheduleClockTimer = null;
  const label = document.getElementById('schedule-current-time');
  if (label) label.style.display = 'none';
}

function scheduleNow() {
  if (!scheduleClockBaseMs) return new Date();
  return new Date(scheduleClockBaseMs + (Date.now() - scheduleClockReceivedMs));
}

function renderScheduleClock() {
  const label = document.getElementById('schedule-current-time');
  const detail = state.schedules.detail;
  if (!label || state.currentView !== 'schedules' || !detail) return;
  const now = scheduleNow();
  let text;
  try {
    text = formatInTimeZone(now, detail.timezone, 'zh-CN', {
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hourCycle: 'h23',
    });
  } catch (e) {
    text = String(detail.server_now || '').slice(11, 19) || '—';
  }
  const tzLabel = timezoneDisplayLabel(detail.timezone);
  label.textContent = `${text} ${tzLabel}`.trim();
  label.style.display = '';
}

function startScheduleClock(detail) {
  stopScheduleClock();
  const parsed = Date.parse(detail && detail.server_now ? detail.server_now : '');
  scheduleClockBaseMs = Number.isFinite(parsed) ? parsed : Date.now();
  scheduleClockReceivedMs = Date.now();
  renderScheduleClock();
  scheduleClockTimer = setInterval(renderScheduleClock, 1000);
}

function scheduleIdentityValue(dateText, timezone) {
  return `${dateText}@@${encodeURIComponent(timezone || '')}`;
}

function scheduleDateOptions(persona) {
  const counts = {};
  (persona.dates || []).forEach(item => {
    counts[item.date] = (counts[item.date] || 0) + 1;
  });
  return (persona.dates || []).map(item => {
    const value = scheduleIdentityValue(item.date, item.timezone);
    const label = counts[item.date] > 1
      ? `${item.date} · ${timezoneDisplayLabel(item.timezone)}`
      : item.date;
    return `<option value="${escapeAttr(value)}" data-date="${escapeAttr(item.date)}" data-tz="${escapeAttr(item.timezone)}">${escapeHtml(label)}</option>`;
  }).join('');
}

function latestScheduleIdentity(persona) {
  const item = [...(persona.dates || [])].reverse().find(entry => entry.date === persona.latest_date)
    || (persona.dates || [])[0];
  return item ? scheduleIdentityValue(item.date, item.timezone) : '';
}

async function renderSchedules() {
  const wrap = document.getElementById('schedules-content');
  const s = state.schedules;
  if (!s.personasLoaded) {
    wrap.innerHTML = '<div class="loading-wrap"><span class="spinner"></span> 加载中...</div>';
    try {
      const resp = await api.getSchedulePersonas();
      if (!resp.success) throw new Error(resp.error || '获取人格列表失败');
      s.personas = resp.personas || [];
      s.personasLoaded = true;
    } catch (e) {
      wrap.innerHTML = `<div class="empty-state">⚠️ ${escapeHtml(e.message)}</div>`;
      return;
    }
  }

  if (!s.personas.length) {
    wrap.innerHTML = '<div class="empty-state">暂无 AI 日程快照。开启 daily_schedule.ai_daily 后生成，或使用 /schedule regenerate。</div>';
    return;
  }

  const personaSelect = document.getElementById('sch-persona');
  const dateSelect = document.getElementById('sch-date');
  const currentKey = personaSelect.value || (s.personas[0] && s.personas[0].key);

  // 填充人格下拉（仅当选项集合变化时重建，保留当前选择）
  const currentKeys = Array.from(personaSelect.options).map(o => o.value);
  const expectedKeys = s.personas.map(p => p.key);
  const needsRebuild = currentKeys.length !== expectedKeys.length ||
      currentKeys.some((v, i) => v !== expectedKeys[i]);
  if (needsRebuild) {
    personaSelect.innerHTML = s.personas.map(p =>
      `<option value="${escapeAttr(p.key)}">${escapeHtml(p.name)}</option>`
    ).join('');
    if (s.personas.some(p => p.key === currentKey)) {
      personaSelect.value = currentKey;
    }
  }

  const persona = s.personas.find(p => p.key === personaSelect.value) || s.personas[0];
  if (!persona) return;

  // date + timezone 才是快照身份；相同日期存在多个时区时必须可分别选择。
  const currentIdentity = dateSelect.value || latestScheduleIdentity(persona);
  dateSelect.innerHTML = scheduleDateOptions(persona);
  dateSelect.value = [...dateSelect.options].some(o => o.value === currentIdentity)
    ? currentIdentity
    : latestScheduleIdentity(persona);

  await loadScheduleDetail(persona.key, dateSelect.value);
}

async function loadScheduleDetail(personaKey, identityValue) {
  const s = state.schedules;
  const wrap = document.getElementById('schedules-content');
  const dateSelect = document.getElementById('sch-date');
  if (!personaKey || !identityValue) {
    s.detail = null;
    s.draft = null;
    wrap.innerHTML = '<div class="empty-state">请选择有效的人格和日期。</div>';
    return;
  }
  const option = [...dateSelect.options].find(o => o.value === identityValue);
  const dateText = option ? option.dataset.date : '';
  const timezone = option ? option.dataset.tz : '';
  if (!dateText) {
    wrap.innerHTML = '<div class="empty-state">请选择有效的人格和日期。</div>';
    return;
  }
  const requestToken = ++s.detailToken;
  wrap.innerHTML = '<div class="loading-wrap"><span class="spinner"></span> 加载中...</div>';
  try {
    const resp = await api.getScheduleDetail(personaKey, dateText, timezone);
    if (requestToken !== s.detailToken) return; // 过期响应：已有更新的选择
    if (!resp.success) throw new Error(resp.error || '获取日程详情失败');
    s.detail = resp.detail;
    s.draft = {
      snapshot_id: resp.detail.snapshot_id,
      persona_hash: personaKey,
      local_date: dateText,
      timezone,
      identity_value: identityValue,
      slots: resp.detail.slots.map(slot => ({ ...slot })),
    };
    s.dirty = false;
    startScheduleClock(resp.detail);
    renderScheduleCards();
  } catch (e) {
    if (requestToken !== s.detailToken) return;
    wrap.innerHTML = `<div class="empty-state">⚠️ ${escapeHtml(e.message)}</div>`;
  }
}

function renderScheduleCards() {
  const s = state.schedules;
  const detail = s.detail;
  const wrap = document.getElementById('schedules-content');
  if (!detail) {
    wrap.innerHTML = '<div class="empty-state">未选择日程。</div>';
    return;
  }
  const editingEnabled = detail.editing_enabled !== false;
  const addTop = document.getElementById('sch-add-slot-top');
  const saveTop = document.getElementById('sch-save-top');
  if (addTop) addTop.style.display = editingEnabled ? '' : 'none';
  if (saveTop) saveTop.style.display = editingEnabled ? '' : 'none';
  const theme = detail.theme || {};
  const themeText = theme.archetype || Object.values(theme).filter(Boolean).join(' · ') || '—';
  // 日界状态摘要：theme 已并入 boundary（daily_theme 承载当日基调）
  const boundary = detail.boundary_state || {};
  const themePart = boundary.daily_theme || themeText || '—';
  const unfinished = Array.isArray(boundary.unfinished_plans) && boundary.unfinished_plans.length
    ? `<div class="boundary-plans">待办：${escapeHtml(boundary.unfinished_plans.join(' · '))}</div>`
    : '';
  const themeHtml = boundary.daily_theme || themeText
    ? `<div class="schedule-boundary">今日主题：${escapeHtml(themePart)}</div>${unfinished}`
    : '';
  const infoLines = [
    `生成日期：${escapeHtml(detail.generated_at ? String(detail.generated_at).slice(0, 10) : '—')}`,
    detail.manually_edited
      ? `✍️ 已人工修改${detail.edited_at ? `（${escapeHtml(formatIsoLocal(detail.edited_at))}）` : ''}`
      : 'AI 生成',
  ];

  const cards = s.draft.slots.map((slot, index) => renderSlotCard(slot, index)).join('');

  const editHint = editingEnabled
    ? `<div class="schedule-edit-hint">AI 生成、静态与已结束的时段只读。想调整未来安排：点右上角「+ 新增时段」添加你自己的时段——可覆盖未来的 AI / 静态时段；在卡片里直接改时间 / 名称 / 状态，改完点「保存修改」。</div>`
    : `<div class="schedule-edit-hint">AI 每日日程未启用，当前快照为只读。</div>`;

  wrap.innerHTML = `
    ${editHint}
    <div class="schedule-meta">${infoLines.map(l => `<div>${l}</div>`).join('')}${themeHtml}</div>
    <div class="schedule-slot-list">${cards || '<div class="empty-state">当日无时段</div>'}</div>
  `;
  bindSlotEvents();
}

function renderSlotCard(slot, index) {
  const mode = slot.edit_mode || (slot.isNew ? 'new' : 'future');
  const editable = (slot.editable_fields || []);
  const canEdit = (field) => editable.includes(field) || slot.isNew;
  // 保存期间锁定输入：避免保存成功重载时静默丢弃窗口内的修改
  const locked = !!state.schedules.saving;
  const lockAttr = locked ? ' disabled' : '';

  const timeHtml = `
    <span class="slot-time">
      ${canEdit('start')
        ? `<input class="slot-input slot-input-time" data-field="start" data-index="${index}" value="${escapeAttr(slot.start)}" title="开始时间 HH:MM"${lockAttr}>`
        : escapeHtml(slot.start)}
      <span class="slot-time-sep">─</span>
      ${canEdit('end')
        ? `<input class="slot-input slot-input-time" data-field="end" data-index="${index}" value="${escapeAttr(slot.end)}" title="结束时间 HH:MM（24:00 合法）"${lockAttr}>`
        : escapeHtml(slot.end)}
    </span>`;

  const nameHtml = canEdit('name')
    ? `<input class="slot-input slot-input-name" data-field="name" data-index="${index}" value="${escapeAttr(slot.name)}" maxlength="24" placeholder="时段名称"${lockAttr}>`
    : `<div class="slot-name-text">${escapeHtml(slot.name || '—')}</div>`;

  const stateHtml = canEdit('state')
    ? `<textarea class="slot-input slot-input-state" data-field="state" data-index="${index}" maxlength="500" rows="2" placeholder="状态描述"${lockAttr}>${escapeHtml(slot.state)}</textarea>`
    : `<div class="slot-state-text">${escapeHtml(slot.state || '—')}</div>`;

  const origin = slot.origin || 'ai';
  const readonlyOrigin = origin === 'static' || origin === 'ai' || origin === 'executed';
  const endEditable = editable.includes('end');
  let badge;
  if (slot.isNew) {
    badge = '新增 · 草稿';
  } else if (mode === 'finished') {
    badge = '已结束 · 只读';
  } else if (mode === 'active') {
    badge = endEditable ? '正在进行 · 仅结束时间可编辑' : '正在进行 · 只读';
  } else if (mode === 'imminent') {
    badge = readonlyOrigin ? '即将开始 · 只读' : '即将开始 · 仅结束时间可改';
  } else if (mode === 'future') {
    badge = readonlyOrigin ? '未来 · 只读' : '未来 · 可编辑';
  } else {
    badge = SLOT_MODE_LABEL[mode] || mode;
  }
  const originLabel = origin === 'static'
    ? '静态'
    : (origin === 'user'
      ? '用户'
      : (origin === 'executed'
        ? `${mode === 'active' ? '正在进行' : '已结束'} · ${slot.source_origin || '历史'}`
        : 'AI'));
  const originCls = `slot-origin slot-origin-${origin}`;
  const cls = `slot-card slot-${mode}`;
  return `
    <div class="${cls}" data-index="${index}">
      <div class="slot-card-header">
        ${timeHtml}
        <span class="slot-badge">${escapeHtml(badge)}</span>
        <span class="${originCls}">${escapeHtml(originLabel)}</span>
        ${(slot.deletable || slot.isNew) && !locked ? `<button class="slot-delete-btn" data-index="${index}" type="button">删除</button>` : ''}
      </div>
      ${nameHtml}
      ${stateHtml}
    </div>
  `;
}

function bindSlotEvents() {
  const s = state.schedules;
  document.querySelectorAll('#schedules-content .slot-input').forEach(input => {
    input.addEventListener('input', () => {
      const index = Number(input.dataset.index);
      const field = input.dataset.field;
      if (!s.draft || s.draft.slots[index] == null) return;
      s.draft.slots[index][field] = input.value;
      s.dirty = true;
    });
  });
  document.querySelectorAll('#schedules-content .slot-delete-btn').forEach(button => {
    button.addEventListener('click', () => {
      if (!s.draft) return;
      s.draft.slots.splice(Number(button.dataset.index), 1);
      s.dirty = true;
      renderScheduleCards();
    });
  });
}

function addNewSlot() {
  const s = state.schedules;
  if (!s.draft || !s.detail) return;
  if (s.detail.editing_enabled === false) {
    toast('error', 'AI 每日日程未启用，当前快照为只读。');
    return;
  }
  const current = scheduleNow();
  const currentDate = formatInTimeZone(current, s.detail.timezone, 'en-CA', {
    year: 'numeric', month: '2-digit', day: '2-digit',
  });
  if (s.detail.local_date < currentDate) {
    toast('error', '历史日期仅允许查看，不能新增时段。');
    return;
  }
  const parts = formatToPartsInTimeZone(current, s.detail.timezone, 'en-GB', {
    hour: '2-digit', hourCycle: 'h23',
  });
  const hour = Number(parts.find(p => p.type === 'hour')?.value || 0);
  // 这里只提供便于填写的默认值；是否可保存仍由服务端按冻结点（下一整分钟）校验。
  const startMinute = Math.min(23 * 60, (hour + 1) * 60);
  const endMinute = Math.min(24 * 60, startMinute + 60);
  const formatMinute = (value) => value === 24 * 60
    ? '24:00'
    : `${String(Math.floor(value / 60)).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`;
  s.draft.slots.unshift({
    slot_ref: '',
    name: '',
    start: formatMinute(startMinute),
    end: formatMinute(endMinute),
    state: '',
    edit_mode: 'new',
    editable_fields: ['start', 'end', 'name', 'state'],
    isNew: true,
    origin: 'user',
  });
  s.dirty = true;
  renderScheduleCards();
}

async function saveScheduleDraft() {
  const s = state.schedules;
  if (!s.draft || !s.detail || s.saving) return;
  if (s.detail.editing_enabled === false) {
    toast('error', 'AI 每日日程未启用，当前快照为只读。');
    return;
  }
  const personaSelect = document.getElementById('sch-persona');
  const dateSelect = document.getElementById('sch-date');
  const payload = {
    persona_hash: s.draft.persona_hash,
    date: s.draft.local_date,
    timezone: s.draft.timezone,
    snapshot_id: s.draft.snapshot_id,
    user_slots: s.draft.slots.filter(slot => (slot.origin || 'ai') === 'user').map(slot => ({
      name: slot.name,
      start: slot.start,
      end: slot.end,
      state: slot.state,
      // 服务端签发并持久化的稳定引用；新增草稿无 id，由服务端签发
      user_slot_id: slot.user_slot_id || '',
    })),
  };
  s.saving = true;
  const saveBtn = document.getElementById('sch-save-top');
  if (saveBtn) saveBtn.disabled = true;
  renderScheduleCards();  // 锁定时段输入（保存期间不可编辑）
  try {
    const resp = await api.saveSchedule(payload);
    if (!resp.success) throw new Error(resp.error || '保存失败');
    toast('success', '已保存。AI 每日补生成不会覆盖人工修改；/schedule regenerate 可恢复 AI 版本。');
    s.dirty = false;
    // 保存只使用草稿身份；仅在页面仍选择同一身份时刷新，否则只提示成功
    if (personaSelect.value === s.draft.persona_hash && dateSelect.value === s.draft.identity_value) {
      await loadScheduleDetail(s.draft.persona_hash, s.draft.identity_value);
    }
  } catch (e) {
    toast('error', e.message);
  } finally {
    s.saving = false;
    if (saveBtn) saveBtn.disabled = false;
    renderScheduleCards();  // 恢复输入状态
  }
}

// ==================== [8c] 静态日程配置 ====================
// 高基数配置不再交给 AstrBot TemplateListEditor：这里只渲染当前页的紧凑行，
// 长状态文本仅在唯一的编辑 modal 中挂载。

function staticSchedulePageItems(slots, page, pageSize) {
  const safeSlots = Array.isArray(slots) ? slots : [];
  const size = Math.max(1, Number(pageSize) || 20);
  const pageCount = Math.max(1, Math.ceil(safeSlots.length / size));
  const safePage = Math.min(Math.max(0, Number(page) || 0), pageCount - 1);
  const start = safePage * size;
  return {
    page: safePage,
    pageCount,
    items: safeSlots.slice(start, start + size).map((slot, offset) => ({
      slot,
      index: start + offset,
    })),
  };
}

async function renderStaticSchedules() {
  if (!state.staticSchedules.loaded) {
    await loadStaticSchedules();
    return;
  }
  renderStaticScheduleTable();
}

async function loadStaticSchedules() {
  const wrap = document.getElementById('static-schedules-content');
  wrap.innerHTML = '<div class="loading-wrap"><span class="spinner"></span> 加载中...</div>';
  try {
    const resp = await api.getStaticSchedules();
    if (!resp.success) throw new Error(resp.error || '获取静态日程失败');
    const s = state.staticSchedules;
    s.revision = String(resp.revision || '');
    s.slots = Array.isArray(resp.slots) ? resp.slots.map(slot => ({ ...slot })) : [];
    s.warnings = Array.isArray(resp.warnings) ? resp.warnings : [];
    s.maxSlots = Number(resp.max_slots) || 256;
    s.page = 0;
    s.dirty = false;
    s.loaded = true;
    renderStaticScheduleTable();
  } catch (e) {
    wrap.innerHTML = `<div class="empty-state">⚠️ ${escapeHtml(e.message)}</div>`;
  }
}

function renderStaticScheduleTable() {
  const s = state.staticSchedules;
  const wrap = document.getElementById('static-schedules-content');
  const paged = staticSchedulePageItems(s.slots, s.page, s.pageSize);
  s.page = paged.page;
  const saveBtn = document.getElementById('static-save');
  if (saveBtn) {
    saveBtn.disabled = !s.dirty || s.saving;
    saveBtn.textContent = s.saving ? '保存中…' : '保存修改';
  }
  ['static-reload', 'static-add-main'].forEach(id => {
    const button = document.getElementById(id);
    if (button) button.disabled = s.saving;
  });

  const warningHtml = s.warnings.length
    ? `<div class="static-warning-list">${s.warnings.map(item => `<div>⚠️ ${escapeHtml(item)}</div>`).join('')}</div>`
    : '';
  const dirtyBadge = s.dirty
    ? '<span class="badge badge-warning">有未保存修改</span>'
    : '<span class="badge badge-success">已保存</span>';
  const rows = paged.items.map(({ slot, index }) => {
    const stateText = String(slot.state_prompt || '').replace(/\s+/g, ' ').trim();
    const preview = stateText.length > 90 ? `${stateText.slice(0, 87)}…` : (stateText || '—');
    return `
      <tr>
        <td data-label="顺序"><span class="static-order">${index + 1}</span></td>
        <td data-label="时间" class="col-time">${escapeHtml(slot.start_time || '')} ─ ${escapeHtml(slot.end_time || '')}</td>
        <td data-label="名称">${escapeHtml(slot.name || '—')}</td>
        <td data-label="状态"><div class="static-state-preview" title="${escapeAttr(stateText)}">${escapeHtml(preview)}</div></td>
        <td class="col-actions" data-label="操作">
          <button class="btn btn-sm static-row-action" data-action="up" data-index="${index}" title="上移"${index === 0 || s.saving ? ' disabled' : ''}>↑</button>
          <button class="btn btn-sm static-row-action" data-action="down" data-index="${index}" title="下移"${index === s.slots.length - 1 || s.saving ? ' disabled' : ''}>↓</button>
          <button class="btn btn-sm static-row-action" data-action="edit" data-index="${index}"${s.saving ? ' disabled' : ''}>编辑</button>
          <button class="btn btn-sm static-row-action static-delete" data-action="delete" data-index="${index}"${s.saving ? ' disabled' : ''}>删除</button>
        </td>
      </tr>`;
  }).join('');
  const pagination = paged.pageCount > 1
    ? `<div class="static-pagination">
        <button class="btn btn-sm" data-static-page="${paged.page - 1}"${paged.page <= 0 ? ' disabled' : ''}>上一页</button>
        <span>第 ${paged.page + 1} / ${paged.pageCount} 页</span>
        <button class="btn btn-sm" data-static-page="${paged.page + 1}"${paged.page >= paged.pageCount - 1 ? ' disabled' : ''}>下一页</button>
      </div>`
    : '';

  wrap.innerHTML = `
    <div class="static-summary">
      <div>共 ${s.slots.length} 个时段（上限 ${s.maxSlots}）· 当前页渲染 ${paged.items.length} 条</div>
      ${dirtyBadge}
    </div>
    ${warningHtml}
    <div class="table-wrap">
      <table class="data-table static-schedule-table">
        <thead><tr><th>顺序</th><th>时间</th><th>名称</th><th>状态描述</th><th class="col-actions">操作</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="5"><div class="empty-state">暂无静态时段；可新增普通时段，或使用睡眠/午餐/晚餐预设。</div></td></tr>'}</tbody>
      </table>
    </div>
    ${pagination}`;
  bindStaticScheduleTableEvents();
}

function bindStaticScheduleTableEvents() {
  document.querySelectorAll('.static-row-action').forEach(button => {
    button.addEventListener('click', () => {
      const index = Number(button.dataset.index);
      const action = button.dataset.action;
      if (action === 'edit') return openStaticSlotModal(index);
      if (action === 'delete') return deleteStaticSlot(index);
      if (action === 'up') return moveStaticSlot(index, -1);
      if (action === 'down') return moveStaticSlot(index, 1);
    });
  });
  document.querySelectorAll('[data-static-page]').forEach(button => {
    button.addEventListener('click', () => {
      state.staticSchedules.page = Number(button.dataset.staticPage) || 0;
      renderStaticScheduleTable();
    });
  });
}

function markStaticScheduleDirty() {
  const s = state.staticSchedules;
  s.dirty = true;
  s.warnings = [];
}

function openStaticSlotModal(index = null, preset = 'time_slot') {
  const s = state.staticSchedules;
  if (s.saving) return;
  const isNew = index === null;
  if (isNew && s.slots.length >= s.maxSlots) {
    toast('error', `静态日程最多允许 ${s.maxSlots} 个时段。`);
    return;
  }
  const PRESETS = {
    time_slot: {
      __template_key: 'time_slot', name: '', start_time: '00:00', end_time: '06:00', state_prompt: '',
    },
    sleep_slot: {
      __template_key: 'sleep_slot', name: '睡眠', start_time: '22:00', end_time: '08:00',
      state_prompt: '刚被消息叫醒或正在半梦半醒之间。按人设自然回应——可带困意、含糊、迟疑、不情愿，但不拒绝对话；允许偶尔走神、把话说半句、打哈欠。',
    },
    lunch_slot: {
      __template_key: 'lunch_slot', name: '午餐', start_time: '12:00', end_time: '13:00',
      state_prompt: '正在吃午餐或刚结束午餐。按人设自然回应——可提及咀嚼、餐具、餐桌氛围、饭后犯困或午间小憩的打算，语气放松从容，不因进食打断对话节奏。',
    },
    dinner_slot: {
      __template_key: 'dinner_slot', name: '晚餐', start_time: '18:30', end_time: '19:30',
      state_prompt: '正在吃晚餐或刚结束晚餐。按人设自然回应——可提及饭菜、餐桌氛围、饭后散步或收拾碗筷，语气松弛，带一点一天将尽的回味感，不打断对话节奏。',
    },
  };
  const slot = isNew
    ? (PRESETS[preset] || PRESETS.time_slot)
    : s.slots[index];
  if (!slot) return;
  s.editingIndex = index;
  document.getElementById('static-slot-modal-title').textContent = isNew ? '新增静态时段' : `编辑静态时段 #${index + 1}`;
  document.getElementById('sf-name').value = slot.name || '';
  document.getElementById('sf-start').value = slot.start_time || '';
  document.getElementById('sf-end').value = slot.end_time || '';
  document.getElementById('sf-state').value = slot.state_prompt || '';
  const modal = document.getElementById('static-slot-modal');
  modal.dataset.templateKey = slot.__template_key || preset || 'time_slot';
  modal.classList.add('active');
  document.getElementById('sf-name').focus();
}

function closeStaticSlotModal() {
  state.staticSchedules.editingIndex = null;
  document.getElementById('static-slot-modal').classList.remove('active');
}

function normalizeStaticTime(value, allow24) {
  const match = String(value || '').trim().match(/^(\d{1,2}):(\d{2})$/);
  if (!match) return null;
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  if (allow24 && hour === 24 && minute === 0) return '24:00';
  if (hour < 0 || hour > 23 || minute < 0 || minute > 59) return null;
  return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
}

function applyStaticSlotModal() {
  const s = state.staticSchedules;
  const start = normalizeStaticTime(document.getElementById('sf-start').value, false);
  const end = normalizeStaticTime(document.getElementById('sf-end').value, true);
  if (!start || !end) {
    toast('error', '时间格式必须为 HH:MM；结束时间允许 24:00。');
    return;
  }
  if (start === end) {
    toast('error', '开始时间和结束时间不能相同。');
    return;
  }
  const slot = {
    __template_key: document.getElementById('static-slot-modal').dataset.templateKey || 'time_slot',
    name: document.getElementById('sf-name').value.trim(),
    start_time: start,
    end_time: end,
    state_prompt: document.getElementById('sf-state').value.trim(),
  };
  const index = s.editingIndex;
  if (index === null) {
    s.slots.push(slot);
    s.page = Math.floor((s.slots.length - 1) / s.pageSize);
  } else if (s.slots[index]) {
    s.slots[index] = slot;
  }
  markStaticScheduleDirty();
  closeStaticSlotModal();
  renderStaticScheduleTable();
}

function moveStaticSlot(index, delta) {
  const s = state.staticSchedules;
  const target = index + delta;
  if (!s.slots[index] || target < 0 || target >= s.slots.length) return;
  [s.slots[index], s.slots[target]] = [s.slots[target], s.slots[index]];
  s.page = Math.floor(target / s.pageSize);
  markStaticScheduleDirty();
  renderStaticScheduleTable();
}

function deleteStaticSlot(index) {
  const s = state.staticSchedules;
  const slot = s.slots[index];
  if (!slot) return;
  showConfirm(
    '删除静态时段？',
    `将从草稿中删除「${escapeHtml(slot.name || `时段 #${index + 1}`)}」。保存前不会写入配置。`,
    () => {
      s.slots.splice(index, 1);
      markStaticScheduleDirty();
      renderStaticScheduleTable();
    },
  );
}

function reloadStaticSchedules() {
  const reload = () => {
    state.staticSchedules.dirty = false;
    state.staticSchedules.loaded = false;
    renderStaticSchedules();
  };
  if (!state.staticSchedules.dirty) return reload();
  showConfirm('放弃未保存的修改？', '重新加载会丢弃当前静态日程草稿。', reload);
}

async function saveStaticSchedules() {
  const s = state.staticSchedules;
  if (!s.dirty || s.saving) return;
  s.saving = true;
  renderStaticScheduleTable();
  try {
    const resp = await api.saveStaticSchedules({
      revision: s.revision,
      slots: s.slots,
    });
    if (!resp.success) throw new Error(resp.error || '保存静态日程失败');
    s.revision = String(resp.revision || '');
    s.slots = Array.isArray(resp.slots) ? resp.slots.map(slot => ({ ...slot })) : [];
    s.warnings = Array.isArray(resp.warnings) ? resp.warnings : [];
    s.maxSlots = Number(resp.max_slots) || s.maxSlots;
    s.dirty = false;
    toast('success', s.warnings.length ? '静态日程已保存；请留意重叠提示。' : '静态日程已保存。');
  } catch (e) {
    toast('error', e.message);
  } finally {
    s.saving = false;
    renderStaticScheduleTable();
  }
}

// ==================== [9] init ====================
function bindNav() {
  document.querySelectorAll('.nav-item').forEach(btn => {
    btn.addEventListener('click', () => switchView(btn.dataset.view));
  });
  document.getElementById('cal-prev').addEventListener('click', () => changeMonth(-1));
  document.getElementById('cal-next').addEventListener('click', () => changeMonth(1));
  document.getElementById('cal-today').addEventListener('click', jumpToToday);
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
  // 新增任务 modal
  document.getElementById('task-create-btn').addEventListener('click', openCreateTaskModal);
  document.getElementById('tf-cancel').addEventListener('click', closeCreateTaskModal);
  // 人格日程
  const applyScheduleIdentity = (personaKey, identityValue, rebuildDates) => {
    if (rebuildDates) {
      const persona = state.schedules.personas.find(p => p.key === personaKey);
      const dateSelect = document.getElementById('sch-date');
      if (persona) {
        dateSelect.innerHTML = scheduleDateOptions(persona);
        dateSelect.value = latestScheduleIdentity(persona);
        identityValue = identityValue || dateSelect.value;
      }
    }
    loadScheduleDetail(personaKey, identityValue);
  };
  const switchScheduleIdentity = (personaKey, identityValue, rebuildDates) => {
    const s = state.schedules;
    const personaSelect = document.getElementById('sch-persona');
    const dateSelect = document.getElementById('sch-date');
    // 切换前记录旧选择：取消确认时回滚，避免选择器与内容脱节
    const prevPersona = (s.draft && s.draft.persona_hash) || personaSelect.value;
    const prevDate = (s.draft && s.draft.identity_value) || dateSelect.value;
    const rollback = () => {
      if (personaSelect.value !== prevPersona) personaSelect.value = prevPersona;
      if (dateSelect.value !== prevDate) dateSelect.value = prevDate;
    };
    if (!s.dirty) {
      applyScheduleIdentity(personaKey, identityValue, rebuildDates);
      return;
    }
    // 先确认再切换：确认后才重建下拉/加载，取消时选择器保持原样
    showConfirm('放弃未保存的修改？', '当前草稿尚未保存，切换人格或日期将丢失这些修改。', () => {
      s.dirty = false;
      applyScheduleIdentity(personaKey, identityValue, rebuildDates);
    }, rollback);
  };
  document.getElementById('sch-persona').addEventListener('change', () => {
    const persona = state.schedules.personas.find(p => p.key === document.getElementById('sch-persona').value);
    if (persona) {
      switchScheduleIdentity(persona.key, '', true);
    }
  });
  document.getElementById('sch-date').addEventListener('change', () => {
    const personaKey = document.getElementById('sch-persona').value;
    switchScheduleIdentity(personaKey, document.getElementById('sch-date').value, false);
  });
  document.getElementById('sch-add-slot-top').addEventListener('click', addNewSlot);
  document.getElementById('sch-save-top').addEventListener('click', saveScheduleDraft);
  // 顶栏刷新：静态日程走完整重载，其余视图重取数据
  document.getElementById('refresh-view').addEventListener('click', () => {
    if (state.currentView === 'static-schedules') {
      reloadStaticSchedules();
      return;
    }
    refreshCurrentView();
  });
  // 静态日程（固定工具栏 + 单一编辑 modal）
  document.getElementById('static-add-main').addEventListener('click', (event) => {
    event.stopPropagation();
    document.getElementById('static-add-menu').classList.toggle('open');
  });
  document.querySelectorAll('#static-add-menu .dropdown-item').forEach(item => {
    item.addEventListener('click', () => {
      document.getElementById('static-add-menu').classList.remove('open');
      openStaticSlotModal(null, item.dataset.preset || 'time_slot');
    });
  });
  document.addEventListener('click', (event) => {
    const dropdown = document.getElementById('static-add-dropdown');
    if (dropdown && !dropdown.contains(event.target)) {
      document.getElementById('static-add-menu').classList.remove('open');
    }
  });
  document.getElementById('static-save').addEventListener('click', saveStaticSchedules);
  document.getElementById('static-reload').addEventListener('click', reloadStaticSchedules);
  document.getElementById('sf-cancel').addEventListener('click', closeStaticSlotModal);
  document.getElementById('sf-submit').addEventListener('click', applyStaticSlotModal);
  document.getElementById('static-slot-modal').addEventListener('click', (e) => {
    if (e.target.id === 'static-slot-modal') closeStaticSlotModal();
  });
  document.getElementById('tf-submit').addEventListener('click', submitCreateTask);
  document.getElementById('task-create-modal').addEventListener('click', (e) => {
    if (e.target.id === 'task-create-modal') closeCreateTaskModal();
  });
  window.addEventListener('beforeunload', (event) => {
    if (!state.schedules.dirty && !state.staticSchedules.dirty) return;
    event.preventDefault();
    event.returnValue = '';
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
      const version = String(resp.version || '').trim();
      document.getElementById('version-tag').textContent = version
        ? (version.toLowerCase().startsWith('v') ? version : `v${version}`)
        : '—';
      document.getElementById('plugin-name-tag').textContent =
        resp.display_name || resp.name || PLUGIN_NAME;
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
