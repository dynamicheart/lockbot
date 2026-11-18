/* global scenarioGroups, data, select, node, actionNames, isFailed, choice, caseName, modelLabel, badge, pickCase, pickRun, icon, readRoute, draw, online, imported, cases, activeCase:writable, activeIndex:writable, activeTab:writable, openStep, datasetId */
/* exported diagnosticDownload, callStatusDot, toolTitle, toolActivityLabel, runEfficiency, renderCaseNavigation, notifyReviewParent, selectedRuns, reviewBusy, userNavigation, buildCurrentBoard */
const selectedRuns = new Map();
const expandedCases = new Set();
let reviewBusy = false;
let userNavigation = false;

function callStatusDot(step, info) {
  const output = info?.output;
  const code = output?.exit_code ?? info?.exit_code;
  const failed = isFailed(step, info);
  const diagnostic = /^native_client_(diagnostic|protocol|stderr)$/.test(step[0]);
  const running = /^(运行中|执行中|等待中|running|pending|in_progress)$/i.test(step[3]);
  const complete = !diagnostic && (code === 0 || output?.installed === true ||
    /^(成功|完成|已返回|调用完成|succeeded|completed|returned)$/i.test(step[3]));
  const state = failed ? 'failed' : running ? 'running' : complete ? 'complete' : 'unknown';
  const label = failed ? '调用失败' : running ? '调用中' : complete ? '调用完成' : diagnostic ? '运行日志' : '状态未记录';
  const dot = node('span', null, 'call-status ' + state);
  dot.title = label;
  dot.setAttribute('role', 'img');
  dot.setAttribute('aria-label', label);
  return dot;
}

function toolTitle(step, info) {
  if (info?.source === 'packaged_cli') return 'CLI 内部 HTTP · ' + (step[0] || '未记录工具名');
  const action = typeof info?.cli_action === 'string' ? info.cli_action :
    typeof info?.input?.action === 'string' ? info.input.action :
    actionNames.has(step[1]) ? step[1] : '';
  return [step[0] || '未记录工具名', action].filter(Boolean).join(' · ');
}

function toolActivityLabel(steps, evidence = []) {
  const internal = evidence.filter(info => info?.source === 'packaged_cli').length;
  const diagnostics = steps.filter(step => ['native_client_diagnostic', 'native_client_protocol',
    'native_client_stderr'].includes(step[0])).length;
  if (diagnostics === steps.length && diagnostics) return '运行日志';
  return internal ? (steps.length - internal - diagnostics) + ' 次模型工具调用 · ' + internal + ' 次 CLI 内部 HTTP' +
    (diagnostics ? ' · ' + diagnostics + ' 项运行日志' : '') :
    diagnostics ? (steps.length - diagnostics) + ' 次工具调用 · ' + diagnostics + ' 项运行日志' :
    steps.length + ' 次工具调用';
}

function diagnosticDownload(artifact) {
  const section = node('section', null, 'evidence-section');
  const valid = artifact && /^[a-f0-9]{64}$/.test(artifact.sha256) &&
    Number.isInteger(artifact.bytes) && artifact.bytes >= 0 && artifact.bytes <= 16 * 1024 * 1024 &&
    /^[a-z][a-z0-9_.-]{0,79}$/.test(artifact.filename);
  if (!valid) {
    section.append(node('p', '诊断文件引用无效。', 'payload-error'));
    return section;
  }
  if (!online || imported) {
    section.append(node('p', '完整诊断保存在独立 evidence 文件中；请在已登录的测试看板下载。', 'payload-empty'));
    return section;
  }
  const button = node('button', '下载完整诊断（' + Math.ceil(artifact.bytes / 1024) + ' KB）', 'quiet-button');
  const status = node('p', '', 'payload-empty');
  button.type = 'button';
  button.onclick = async () => {
    button.disabled = true;
    status.textContent = '';
    try {
      const token = localStorage.getItem('lockbot_token');
      if (!token) throw new Error('请先登录超级管理员。');
      const response = await fetch('/api/dev/trace-review/evidence/' + artifact.sha256, {
        headers: {Authorization: 'Bearer ' + token}, cache: 'no-store', redirect: 'error',
        signal: AbortSignal.timeout(30000),
      });
      if (response.status === 401 || response.status === 403) throw new Error('当前账号无权下载。');
      if (!response.ok) throw new Error('文件未上传或完整性检查失败。');
      if (response.headers.get('X-Content-SHA256') !== artifact.sha256) throw new Error('文件摘要不匹配。');
      const bytes = await response.arrayBuffer();
      if (bytes.byteLength !== artifact.bytes) throw new Error('文件长度不匹配。');
      if (globalThis.crypto?.subtle) {
        const hash = await globalThis.crypto.subtle.digest('SHA-256', bytes);
        const actual = [...new Uint8Array(hash)].map(value => value.toString(16).padStart(2, '0')).join('');
        if (actual !== artifact.sha256) throw new Error('文件摘要不匹配。');
      }
      if (localStorage.getItem('lockbot_token') !== token) throw new Error('登录状态已改变，请重试。');
      const url = URL.createObjectURL(new Blob([bytes], {type: 'application/octet-stream'}));
      const link = node('a');
      link.href = url;
      link.download = artifact.filename;
      document.body.append(link);
      try { link.click(); } finally { link.remove(); URL.revokeObjectURL(url); }
      status.textContent = '完整诊断已下载；看板记录与评论未改变。';
    } catch (error) {
      status.textContent = error.message || '下载失败，请重试。';
    } finally {
      button.disabled = false;
    }
  };
  section.append(button, status);
  return section;
}

function runEfficiency(trial) {
  const tools = trial.sessions.flat().filter(entry => entry.role === 'tools');
  const steps = tools.flatMap(entry => entry.steps);
  const completeTools = tools.every(entry => !entry.total || entry.total === entry.steps.length);
  const parts = [];
  const phases = Array.isArray(trial.metrics?.phases) ? trial.metrics.phases : [];
  const measured = phases.filter(phase => Number.isInteger(phase.model_tool_calls));
  if (measured.length) {
    const partial = measured.length < phases.length ? '（部分阶段）' : '';
    parts.push('模型工具 ' + measured.reduce((sum, phase) => sum + phase.model_tool_calls, 0) + ' 次' + partial);
    const executed = measured.filter(phase => Number.isInteger(phase.cli_calls));
    if (executed.length) parts.push('CLI ' + executed.reduce((sum, phase) => sum + phase.cli_calls, 0) +
      ' 次' + (executed.length < phases.length ? '（部分阶段）' : ''));
  } else if (trial.sessions.length && completeTools) {
    parts.push('CLI ' + steps.filter(step => ['run_lockbot', 'packaged_cli.main'].includes(step[0])).length + ' 次');
  }
  const http = phases.filter(phase => Number.isInteger(phase.http_requests));
  if (http.length) {
    const count = http.reduce((sum, phase) => sum + phase.http_requests, 0);
    parts.push('HTTP ' + count + ' 次' + (http.length < phases.length ? '（部分阶段）' : ''));
  }
  const timings = phases.filter(phase => Number.isFinite(phase.duration_seconds));
  if (Number.isFinite(trial.elapsed_seconds)) parts.push('任务耗时 ' + trial.elapsed_seconds.toFixed(1) + ' 秒');
  else if (phases.length && timings.length === phases.length) {
    parts.push('任务耗时 ' + timings.reduce((sum, phase) => sum + phase.duration_seconds, 0).toFixed(1) + ' 秒');
  }
  return parts.join(' · ');
}

function renderCaseNavigation() {
  const sidebar = document.querySelector('.sidebar');
  const scroll = sidebar.scrollTop;
  const groups = scenarioGroups().map(([label, keys]) => {
    const group = node('div', null, 'case-group');
    if (label) group.append(node('h3', label));
    for (const key of keys) {
      const runs = data.cases[key];
      const item = node('div', null, 'case-item');
      item.dataset.reviewCase = key;
      item.dataset.reviewScope = 'case';
      item.dataset.reviewField = '测例名称';
      const row = node('div', null, 'case-item-row');
      const multi = runs.length > 1;
      if (multi) {
        const toggle = node('button', null, 'case-expand');
        toggle.type = 'button';
        toggle.append(icon('right'));
        toggle.setAttribute('aria-expanded', expandedCases.has(key));
        toggle.setAttribute('aria-label', (expandedCases.has(key) ? '收起' : '展开') + caseName(key) + '的模型');
        toggle.onclick = () => {
          if (expandedCases.has(key)) expandedCases.delete(key); else expandedCases.add(key);
          renderCaseNavigation();
        };
        row.append(toggle);
      }
      const state = !multi && runs[0].state ? badge(runs[0]) : null;
      const button = choice(caseName(key), state, activeCase === key, () => pickCase(key));
      button.dataset.navKind = 'case';
      if (data.updated_cases?.includes(key)) {
        const dot = node('span', null, 'updated-dot');
        dot.title = '已更新'; dot.setAttribute('role', 'img'); dot.setAttribute('aria-label', '已更新');
        button.prepend(dot);
      }
      row.append(button);
      item.append(row);
      if (multi && expandedCases.has(key)) {
        const list = node('div', null, 'case-runs');
        list.setAttribute('role', 'group');
        list.setAttribute('aria-label', caseName(key) + '的模型');
        list.append(...runs.map((run, index) => {
          const button = choice(modelLabel(run, index, key), run.state ? badge(run) : null,
            activeCase === key && activeIndex === index, () => {
              activeCase = key;
              pickRun(index);
            });
          button.dataset.navKind = 'run';
          button.dataset.reviewCase = key;
          button.dataset.reviewScope = 'run';
          button.dataset.reviewIndex = String(index);
          button.dataset.reviewAnchor = 'run';
          button.dataset.reviewField = '模型名称';
          button.title = run.model || '';
          return button;
        }));
        item.append(list);
      }
      group.append(item);
    }
    return group;
  });
  select('case-nav').replaceChildren(...groups);
  sidebar.scrollTop = scroll;
}

function reviewContext(scope = 'run', caseId = activeCase, index = activeIndex) {
  const selectedIndex = data.cases[caseId]?.[index] ? index : 0;
  const trial = data.cases[caseId]?.[selectedIndex];
  const target = data.review_targets?.[caseId]?.[selectedIndex];
  const sourceDataset = target?.dataset_id ?? datasetId;
  return {
    scope,
    ...(online && !imported && sourceDataset ? {dataset_id:sourceDataset} : {}),
    case_id: caseId,
    case_name: caseName(caseId),
    run_index: scope === 'case' ? null : target?.run_index ?? selectedIndex,
    run_id: scope === 'case' || imported ? null : data.review_runs?.[caseId]?.[selectedIndex] ?? null,
    model: scope === 'case' ? '' : trial?.model || '',
    server_backed: online && !imported && !!data.review_runs,
  };
}
function parentMessage(message) {
  if (parent !== window) parent.postMessage(message, location.origin);
}
function notifyReviewParent() {
  parentMessage({ type: 'lockbot-trace-route',
    route: Object.fromEntries(new URLSearchParams(location.hash.slice(1))),
    navigation: userNavigation ? 'push' : 'replace', context: reviewContext() });
  userNavigation = false;
}
function requestReview(anchor, quote = '', fieldLabel = '', options = {}) {
  let scope = options.scope || (anchor === 'case' ? 'case' : 'run');
  let subject = options.subject || (anchor.startsWith('ui:') ? 'interface' : fieldLabel === '判定标准' ? 'grading' : 'general');
  if (anchor.startsWith('message:')) {
    const [, session, entry] = anchor.split(':');
    if (data.cases[activeCase]?.[activeIndex]?.sessions[Number(session)]?.[Number(entry)]?.role === 'user') {
      scope = 'case';
      subject = 'prompt';
    }
  }
  const context = reviewContext(scope, options.caseId || activeCase, options.runIndex);
  const message = { type: 'lockbot-trace-comment', context, anchor: scope === 'case' ? 'case' : anchor,
    quote, field_label: fieldLabel, subject };
  if (parent !== window) parentMessage(message);
  else if (online) {
    try { sessionStorage.setItem('lockbot_trace_pending_comment', JSON.stringify(message)); } catch { /* Storage can be unavailable; keep the current input usable. */ }
    location.href = '/#/agent-traces?' + new URLSearchParams(Object.fromEntries(new URLSearchParams(location.hash.slice(1))));
  } else {
    offlineReview(message);
  }
}
select('review-case').onclick = () => requestReview('case', '', '测例', { scope: 'case' });

const inspectorFeedback = node('button', '评论', 'quiet-button');
inspectorFeedback.type = 'button';
inspectorFeedback.onclick = () => requestReview(select('inspector-fields').dataset.reviewAnchor || 'run', '', '工具调用');
select('inspector-close').before(inspectorFeedback);

function offlineReview(message) {
  const dialog = node('dialog', null, 'offline-review');
  const heading = node('h3', '评论');
  const note = node('p', '离线评论会导出为文件，可交给我继续处理。', 'evidence-note');
  const quote = node('blockquote', message.quote);
  const input = node('textarea');
  input.rows = 5;
  input.maxLength = 4000;
  input.setAttribute('aria-label', '评论内容');
  const save = node('button', '导出评论', 'quiet-button');
  const cancel = node('button', '取消', 'quiet-button');
  save.type = cancel.type = 'button';
  save.onclick = () => {
    if (!input.value.trim()) return input.focus();
    const url = URL.createObjectURL(new Blob([JSON.stringify({ ...message, body: input.value }, null, 2)],
      { type: 'application/json' }));
    const link = node('a');
    link.href = url;
    link.download = 'lockbot-review-comment.json';
    link.click();
    URL.revokeObjectURL(url);
    dialog.close();
  };
  cancel.onclick = () => dialog.close();
  dialog.addEventListener('close', () => dialog.remove());
  dialog.append(heading, note, quote, input, save, cancel);
  document.body.append(dialog);
  dialog.showModal();
  input.focus();
}

const selectionComment = node('button', '评论选中文字', 'selection-comment quiet-button');
selectionComment.type = 'button';
selectionComment.hidden = true;
let capturedSelection;
function anchoredElement(value) {
  let element = value?.nodeType === Node.ELEMENT_NODE ? value : value?.parentElement;
  while (element) {
    const anchor = element.closest('[data-review-anchor], [data-review-case]');
    if (anchor) return anchor;
    element = element.getRootNode()?.host;
  }
  return null;
}
function showSelectionComment(event) {
  if (event.target === selectionComment || document.body.classList.contains('resizing-sidebar')) return;
  const selection = window.getSelection();
  const quote = selection?.toString().trim();
  if (!quote || quote.length > 4000) {
    selectionComment.hidden = true;
    return;
  }
  const anchor = anchoredElement(selection.anchorNode) ||
    event.composedPath().find(element => element instanceof Element && anchoredElement(element));
  const target = anchor && (anchoredElement(anchor) || anchor);
  const origin = selection.anchorNode?.nodeType === Node.ELEMENT_NODE ? selection.anchorNode : selection.anchorNode?.parentElement;
  const ui = origin?.closest('[id]');
  const uiId = ['case-title', 'run-title', 'counts', 'chat-tab', 'event-tab', 'case-nav'].includes(ui?.id) ? ui.id : null;
  const scope = target?.dataset.reviewScope || (uiId === 'case-title' ? 'case' : undefined);
  const runIndex = target?.dataset.reviewIndex;
  capturedSelection = {
    anchor: target?.dataset.reviewAnchor || (uiId ? 'ui:' + uiId : 'run'),
    quote,
    label: target?.dataset.reviewField || '选中文字',
    caseId: target?.dataset.reviewCase,
    scope,
    subject: scope === 'case' ? 'scenario' : undefined,
    runIndex: /^\d+$/.test(runIndex || '') ? Number(runIndex) : undefined,
  };
  const dialog = origin?.closest('dialog') || event.composedPath().find(element => element instanceof Element && element.matches('dialog[open]'));
  (dialog?.open ? dialog : document.body).append(selectionComment);
  const rectangle = selection.rangeCount ? selection.getRangeAt(0).getBoundingClientRect() : null;
  selectionComment.style.left = Math.max(8, Math.min(innerWidth - 150, event.clientX || rectangle?.left || 8)) + 'px';
  selectionComment.style.top = Math.max(8, Math.min(innerHeight - 48, (event.clientY || rectangle?.bottom || 8) + 10)) + 'px';
  selectionComment.hidden = false;
}
selectionComment.onpointerdown = event => event.preventDefault();
function submitSelectionComment() {
  const selected = capturedSelection;
  if (!selected) return;
  requestReview(selected.anchor, selected.quote, selected.label, {
    caseId: selected.caseId, scope: selected.scope, subject: selected.subject, runIndex: selected.runIndex,
  });
  selectionComment.hidden = true;
}
selectionComment.onclick = submitSelectionComment;
document.addEventListener('pointerup', showSelectionComment);
document.addEventListener('keyup', event => { if (event.key !== 'Escape') showSelectionComment(event); });

function locateReview(message) {
  const anchor = typeof message.anchor === 'string' ? message.anchor : '';
  const ui = /^ui:(case-title|run-title|counts|chat-tab|event-tab|case-nav|payload-tab)$/.test(anchor);
  if (anchor.startsWith('ui:') && !ui) return;
  if (!ui && (message.run_id !== reviewContext().run_id ||
      (message.dataset_id ?? null) !== (reviewContext().dataset_id ?? null))) return;
  const parts = String(message.anchor).split(':');
  if(parts[0]==='message' && activeTab!=='chat') {
    activeTab='chat';
    draw();
  } else if(parts[0]==='tool') {
    const entry=data.cases[activeCase]?.[activeIndex]?.sessions[Number(parts[1])]?.[Number(parts[2])];
    const toolIndex=Number(parts[3]);
    if(entry?.role==='tools' && entry.steps[toolIndex]) openStep(entry.steps[toolIndex],entry.evidence?.[toolIndex],parts.slice(0,4).join(':')+':name');
  }
  const marked = [...document.querySelectorAll('[data-review-anchor]')]
    .find(element => element.dataset.reviewAnchor === message.anchor);
  const target = ui ? marked || select(parts[1]) : marked || select('case-title');
  if (target) {
    target.closest('details')?.setAttribute('open', '');
    target.scrollIntoView({ block: 'center', behavior: 'smooth' });
    target.classList.add('review-highlight');
    setTimeout(() => target.classList.remove('review-highlight'), 1800);
  }
}

addEventListener('message', event => {
  if (event.origin !== location.origin || event.source !== parent || parent === window) return;
  const message = event.data;
  if (message?.type === 'lockbot-trace-theme') {
    document.documentElement.classList.toggle('dark', message.dark === true);
  } else if (message?.type === 'lockbot-trace-review-open') {
    reviewBusy = message.open === true;
  } else if (message?.type === 'lockbot-trace-set-route' && message.route && typeof message.route === 'object') {
    const route = new URLSearchParams();
    if (typeof message.route.case === 'string' && message.route.case.length <= 256) route.set('case', message.route.case);
    if (/^\d{1,5}$/.test(message.route.run)) route.set('run', message.route.run);
    if (message.route.view === 'steps') route.set('view', 'steps');
    if (/^\d{1,5}$/.test(message.route.step)) route.set('step', message.route.step);
    if (location.hash.slice(1) !== route.toString()) {
      history.replaceState(null, '', '#' + route);
      readRoute();
      if (cases.includes(activeCase) && !document.querySelector('.layout').hidden) draw();
    }
  } else if (message?.type === 'lockbot-trace-locate') {
    locateReview(message);
  }
});

const resize = select('sidebar-resize');
const maximumWidth = () => Math.max(220, Math.min(560, innerWidth - 420));
function setSidebarWidth(value) {
  const width = Math.min(maximumWidth(), Math.max(220, value));
  document.documentElement.style.setProperty('--case-width', width + 'px');
  resize.setAttribute('aria-valuemin', 220);
  resize.setAttribute('aria-valuemax', maximumWidth());
  resize.setAttribute('aria-valuenow', width);
  try { localStorage.setItem('lockbot_trace_sidebar_width', String(width)); } catch { /* Storage can be unavailable; keep the current input usable. */ }
}
let startWidth, startPointer;
resize.onpointerdown = event => {
  startPointer = event.clientX;
  startWidth = document.querySelector('.sidebar').getBoundingClientRect().width;
  resize.setPointerCapture(event.pointerId);
  document.body.classList.add('resizing-sidebar');
};
resize.onpointermove = event => {
  if (resize.hasPointerCapture(event.pointerId)) setSidebarWidth(startWidth + event.clientX - startPointer);
};
resize.onpointerup = resize.onpointercancel = event => {
  if (resize.hasPointerCapture(event.pointerId)) resize.releasePointerCapture(event.pointerId);
  document.body.classList.remove('resizing-sidebar');
};
resize.ondblclick = () => setSidebarWidth(280);
resize.onkeydown = event => {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
  event.preventDefault();
  const width = document.querySelector('.sidebar').getBoundingClientRect().width;
  setSidebarWidth(event.key === 'Home' ? 220 : event.key === 'End' ? maximumWidth() : width + (event.key === 'ArrowLeft' ? -16 : 16));
};
let savedWidth = 280;
try { savedWidth = Number(localStorage.getItem('lockbot_trace_sidebar_width')) || 280; } catch { /* Storage can be unavailable; keep the current input usable. */ }
setSidebarWidth(savedWidth);

function buildCurrentBoard(snapshots) {
  if (!snapshots.length) throw new Error('No published results');
  const base = snapshots[0].data;
  const board = {...base, title:'LockBot 轨迹浏览器', cases:{}, review_runs:{}, review_targets:{}, updated_cases:[]};
  delete board.dataset_id;
  const grouped = new Map();
  snapshots.forEach(({data: snapshot, metadata}, sourceIndex) => {
    for (const field of ['labels','criteria','groups']) board[field] = {...board[field], ...snapshot[field]};
    for (const [key, trials] of Object.entries(snapshot.cases)) {
      if (!grouped.has(key)) grouped.set(key, new Map());
      const families = new Map();
      trials.forEach((trial, index) => {
        const family = (trial.model || '').replace(/ · 阶段 \d+ 语义复核$/, '');
        if (!families.has(family)) families.set(family, []);
        families.get(family).push({trial, id:snapshot.review_runs?.[key]?.[index],
          target:{dataset_id:metadata.dataset_id, run_index:index, title:metadata.title}});
      });
      for (const [family, entries] of families) grouped.get(key).set(family, entries);
      if (sourceIndex > 0) board.updated_cases.push(key);
    }
  });
  for (const [key, families] of grouped) {
    const entries = [...families.values()].flat();
    board.cases[key] = entries.map(entry => entry.trial);
    board.review_runs[key] = entries.map(entry => entry.id);
    board.review_targets[key] = entries.map(entry => entry.target);
  }
  board.updated_cases = [...new Set(board.updated_cases)];
  return board;
}
