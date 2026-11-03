'use strict';

(() => {
  const fragment = location.hash;
  history.replaceState(null, '', '/me/');
  let ticket = new URLSearchParams(fragment.slice(1)).get('ticket');
  document.addEventListener('DOMContentLoaded', () => {
    const app = document.getElementById('app');
    let session = {authenticated: false, identities: []};
    let busy = false;
    let current = 'mine';
    let groups = [];

    function element(tag, text, className) {
      const node = document.createElement(tag);
      if (text !== undefined) node.textContent = String(text);
      if (className) node.className = className;
      return node;
    }

    async function api(path, body) {
      const response = await fetch(`/me/${path}`, {
        method: body === undefined ? 'GET' : 'POST',
        credentials: 'same-origin', cache: 'no-store', redirect: 'error',
        referrerPolicy: 'no-referrer',
        headers: body === undefined ? {} : {'Content-Type': 'application/json'},
        ...(body === undefined ? {} : {body: JSON.stringify(body)}),
      });
      if (!response.ok) {
        const error = new Error(response.status === 401 ? '会话已失效，请从已绑定的机器人重新获取登录链接。' : '请求未完成，请稍后重试；登录链接也可能已失效。');
        error.status = response.status;
        throw error;
      }
      return response.status === 204 ? {} : response.json();
    }

    function action(label, handler, primary = false) {
      const button = element('button', label, `btn${primary ? ' primary' : ''}`);
      button.type = 'button';
      button.addEventListener('click', async () => {
        if (busy) return;
        busy = true;
        const buttons = [...app.querySelectorAll('button')];
        buttons.forEach(item => { item.disabled = true; });
        try { await handler(); } catch (error) { showError(error); }
        finally {
          busy = false;
          buttons.forEach(item => { if (item.isConnected) item.disabled = false; });
        }
      });
      return button;
    }

    function showError(error) {
      app.querySelector('[role="alert"]')?.remove();
      const message = element('p', error.message, 'notice warning');
      message.setAttribute('role', 'alert');
      app.append(message);
    }

    function identityCard(identity) {
      const card = element('section', undefined, 'panel');
      const fields = [['身份服务', 'issuer'], ['平台', 'platform'], ['身份命名空间', 'realm'], ['账号标识', 'subject'], ['授权范围', 'scope']];
      fields.forEach(([label, key]) => {
        const value = key === 'scope' && Array.isArray(identity.scopes) ? identity.scopes.join('、') : identity[key];
        // Identity metadata is untrusted text, never HTML or a navigable URL.
        card.append(element('p', `${label}：${typeof value === 'string' ? value : JSON.stringify(value ?? '未提供')}`, 'group-meta'));
      });
      return card;
    }

    function header() {
      app.replaceChildren();
      document.getElementById('navigation').replaceChildren();
      const row = element('div', undefined, 'page-heading');
      row.append(element('h1', {mine: '我的占用', groups: '资源群', connections: '我的连接'}[current]));
      const actions = element('div', undefined, 'actions');
      row.append(actions);
      if (session.authenticated) actions.append(action('退出此浏览器', async () => {
        await api('logout', {});
        ticket = null;
        session = {authenticated: false, identities: []};
        renderSignedOut();
      }));
      app.append(row);
    }

    function renderSignedOut() {
      header();
      document.getElementById('navigation').replaceChildren();
      app.append(element('p', '首次使用或换设备时，在你自己的 Agent 私聊中说“打开我的资源台”，再用本浏览器确认登录。'));
      app.append(element('p', '登录后收藏此固定地址，下次直接打开即可。无需提供群 Key 或网页验证码。', 'scope'));
    }

    function fresh(group) {
      return ['ok', 'fresh', 'active', 'ready', 'available', 'healthy'].includes(group.status);
    }

    function queued(allocation) {
      return ['reservation', 'queue', 'queued'].includes(allocation.kind);
    }

    function entries(group) {
      return Array.isArray(group.allocations) ? group.allocations : [];
    }

    function date(value) {
      if (value === null || value === undefined) return '未提供';
      const time = new Date(typeof value === 'number' ? value * 1000 : value);
      return Number.isNaN(time.getTime()) ? '未知' : time.toLocaleString('zh-CN');
    }

    function stat(label, value, unit) {
      const box = element('div', undefined, 'stat');
      const number = element('div', value, 'stat-value');
      number.append(element('span', unit));
      box.append(element('div', label, 'stat-label'), number);
      return box;
    }

    function table(parent, rows, booking) {
      const panel = element('section', undefined, 'panel');
      panel.append(element('h2', `${booking ? '预约与队列' : '持有资源'} · ${rows.length}`, 'panel-title'));
      if (!rows.length) {
        panel.append(element('p', '已确认的群中暂无记录。', 'empty'));
      } else {
        const scroll = element('div', undefined, 'table-scroll');
        const grid = element('table');
        const head = element('thead');
        const heading = element('tr');
        ['资源群', '节点 / 卡号', '状态', '本人身份', '到期时间', '源数据时间'].forEach(label => heading.append(element('th', label)));
        head.append(heading);
        const body = element('tbody');
        for (const {group, allocation} of rows) {
          const row = element('tr');
          const values = [group.label, `${allocation.node ?? '未知节点'}${allocation.device == null ? '' : ' / 卡 ' + allocation.device}`,
            fresh(group) ? (booking ? '预约中' : '持有中') : '待确认', group.identity?.subject,
            date(allocation.expires_at), date(group.source_at)];
          values.forEach(value => row.append(element('td', value ?? '—')));
          body.append(row);
        }
        grid.append(head, body);
        scroll.append(grid);
        panel.append(scroll);
      }
      parent.append(panel);
    }

    function renderMine() {
      const known = groups.filter(fresh);
      const stats = element('div', undefined, 'stats');
      const machines = known.reduce((count, group) => count + new Set(entries(group).filter(a => !queued(a) && a.node != null).map(a => a.node)).size, 0);
      const cards = known.reduce((count, group) => count + new Set(entries(group).filter(a => !queued(a) && a.device != null && a.node != null).map(a => JSON.stringify([a.node, a.device]))).size, 0);
      const bookings = known.reduce((count, group) => count + entries(group).filter(queued).length, 0);
      stats.append(stat('持有机器', machines, '台'), stat('持有卡', cards, '张'), stat('预约', bookings, '项'));
      app.append(stats, element('p', `已更新 ${known.length} / ${groups.length} 个群 · 仅统计已确认占用，机器按群计数`, 'scope'));
      if (known.length !== groups.length) app.append(element('p', `${groups.length - known.length} 个群的数据待确认，统计可能不完整。`, 'notice warning'));
      const rows = groups.flatMap(group => entries(group).map(allocation => ({group, allocation})));
      table(app, rows.filter(row => !queued(row.allocation)), false);
      table(app, rows.filter(row => queued(row.allocation)), true);
    }

    function renderGroups() {
      const grid = element('div', undefined, 'group-grid');
      for (const group of groups) {
        const card = element('section', undefined, 'group-card');
        const heading = element('div', undefined, 'page-heading');
        heading.append(element('h2', group.label || '未命名群'), element('span', fresh(group) ? '已更新' : '待确认', `badge${fresh(group) ? '' : ' warn'}`));
        card.append(heading, element('p', `${group.identity?.platform || ''} · ${group.identity?.subject || ''}`, 'group-meta'));
        const metrics = element('div', undefined, 'group-metrics');
        for (const [label, value] of [['持有记录', entries(group).filter(a => !queued(a)).length], ['预约记录', entries(group).filter(queued).length]]) {
          const item = element('div');
          item.append(element('strong', fresh(group) ? value : '—'), element('span', label));
          metrics.append(item);
        }
        card.append(metrics, element('p', `源数据时间 ${date(group.source_at)}`, 'group-meta'));
        if (!fresh(group)) card.append(element('p', '数据待恢复后确认。', 'notice warning'));
        grid.append(card);
      }
      app.append(grid);
    }

    function renderConnections() {
      app.append(element('p', '连接由你的 IM 绑定授权。添加群时，在自己的 Agent 私聊中说“打开我的资源台”，在本浏览器确认新增授权。', 'scope'));
      for (const identity of session.identities) {
        const card = element('section', undefined, 'group-card');
        card.append(element('h2', `${identity.platform} · ${identity.subject}`));
        const labels = groups.filter(group => ['issuer', 'platform', 'realm', 'subject'].every(key => group.identity?.[key] === identity[key])).map(group => group.label);
        card.append(element('p', `已授权资源群：${labels.join('、') || '尚未读取'}`, 'group-meta'));
        const details = element('details');
        details.append(element('summary', '身份详情'), identityCard(identity));
        card.append(details);
        app.append(card);
      }
      for (const indices of session.same_person_groups || []) {
        const accounts = indices.map(i => session.identities[i]).filter(Boolean).map(identity => `${identity.platform} · ${identity.subject}`);
        if (accounts.length >= 2) app.append(element('p', `组织目录确认属于同一人：${accounts.join('、')}。各群授权分别生效。`, 'group-meta'));
      }
    }

    function render() {
      header();
      const navigation = document.getElementById('navigation');
      navigation.replaceChildren();
      for (const [id, label] of [['mine', '我的占用'], ['groups', '资源群'], ['connections', '我的连接']]) {
        const button = action(label, async () => { current = id; render(); });
        button.className = current === id ? 'active' : '';
        button.setAttribute('aria-current', current === id ? 'page' : 'false');
        navigation.append(button);
      }
      app.querySelector('.page-heading .actions').prepend(action('刷新资源', resources));
      if (!groups.length) app.append(element('p', '当前没有已授权的群资源。', 'empty'));
      ({mine: renderMine, groups: renderGroups, connections: renderConnections})[current]();
    }

    async function resources() {
      const data = await api('resources');
      if (!Array.isArray(data.groups)) throw new Error('资源响应无效，请稍后重试。');
      groups = data.groups;
      render();
    }

    async function confirmTicket() {
      const pendingTicket = ticket;
      const preview = await api('preview', {ticket: pendingTicket});
      if (!preview.identity || typeof preview.identity !== 'object') throw new Error('无法确认待登录身份，请重新获取登录链接。');
      header();
      app.append(element('h2', '确认登录身份', 'panel-title'));
      if (session.authenticated) {
        app.append(element('p', '此浏览器当前身份：', 'scope'));
        session.identities.forEach(identity => app.append(identityCard(identity)));
      }
      app.append(element('p', '本次链接身份：', 'scope'), identityCard(preview.identity));
      app.append(element('p', '只有点击确认后才使用此链接；仅打开页面不会登录。', 'scope'));
      const buttons = element('div', undefined, 'actions');
      const login = async mode => {
        await api('login', {ticket: pendingTicket, mode});
        ticket = null;
        await loadSession();
        if (!session.authenticated) throw new Error('登录后未检测到会话，请检查浏览器 Cookie 设置。');
        await resources();
      };
      buttons.append(action(session.authenticated ? '切换为本次身份' : '确认登录', () => login('replace'), true));
      buttons.append(action('取消', async () => {
        ticket = null;
        if (session.authenticated) await resources(); else renderSignedOut();
      }));
      app.append(buttons);
      if (session.authenticated) {
        app.append(element('p', '只有你控制以上两份绑定并希望在当前浏览器同时查看时，才选择合看。这不会认定两个账号是企业内同一自然人。', 'notice warning'));
        const label = element('label', undefined, 'field');
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.style.width = 'auto';
        label.append(checkbox, document.createTextNode(' 我明确选择仅在此浏览器合看以上身份'));
        app.append(label);
        const append = action('确认在此浏览器合看', async () => {
          if (!checkbox.checked) return;
          await login('append');
        });
        append.hidden = true;
        checkbox.addEventListener('change', () => { append.hidden = !checkbox.checked; });
        app.append(append);
      }
    }

    async function loadSession() {
      session = await api('session');
      if (!Array.isArray(session.identities)) throw new Error('会话响应无效，请稍后重试。');
    }

    async function openPanel() {
      busy = true;
      try {
        await loadSession();
        if (ticket) await confirmTicket();
        else if (session.authenticated) await resources();
        else renderSignedOut();
      } catch (error) {
        // An expired handoff must not hide an existing usable browser session.
        if (session.authenticated) {
          try { await resources(); } catch (resourceError) { showError(resourceError); }
        } else renderSignedOut();
        showError(error);
      } finally {
        busy = false;
      }
    }

    window.addEventListener('hashchange', () => {
      const incoming = new URLSearchParams(location.hash.slice(1)).get('ticket');
      history.replaceState(null, '', '/me/');
      if (!incoming) return;
      if (busy) {
        showError(new Error('当前操作进行中，请完成后重新打开登录链接。'));
        return;
      }
      ticket = incoming;
      void openPanel();
    });
    void openPanel();
  }, {once: true});
})();
