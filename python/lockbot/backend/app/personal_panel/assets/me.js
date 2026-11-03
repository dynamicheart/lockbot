'use strict';

(() => {
  const fragment = location.hash;
  history.replaceState(null, '', '/me/');
  let ticket = new URLSearchParams(fragment.slice(1)).get('ticket');
  document.addEventListener('DOMContentLoaded', () => {
    const app = document.getElementById('app');
    let session = {authenticated: false, identities: []};
    let busy = false;

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
      const row = element('div', undefined, 'page-heading');
      row.append(element('h1', '我的资源'));
      if (session.authenticated) row.append(action('退出此浏览器', async () => {
        await api('logout', {});
        ticket = null;
        session = {authenticated: false, identities: []};
        renderSignedOut();
      }));
      app.append(row);
    }

    function renderSignedOut() {
      header();
      app.append(element('p', '首次使用或换设备时，在你自己的 Agent 私聊中说“打开我的资源台”，再用本浏览器确认登录。'));
      app.append(element('p', '登录后收藏此固定地址，下次直接打开即可。无需提供群 Key 或网页验证码。', 'scope'));
    }

    async function resources() {
      header();
      app.append(element('p', '固定入口：/me/ · 此页面只读，展示当前浏览器已确认的身份授权资源。', 'scope'));
      const identities = element('details');
      identities.append(element('summary', '查看当前浏览器身份'));
      session.identities.forEach(identity => identities.append(identityCard(identity)));
      app.append(identities);
      app.append(action('刷新资源', resources));
      const data = await api('resources');
      if (!Array.isArray(data.groups)) throw new Error('资源响应无效，请稍后重试。');
      if (!data.groups.length) {
        app.append(element('p', '当前没有已授权的群资源。', 'empty'));
        return;
      }
      for (const group of data.groups) {
        const panel = element('section', undefined, 'panel');
        panel.append(element('h2', group.label || '未命名群', 'panel-title'));
        const status = typeof group.status === 'string' ? group.status : 'unknown';
        panel.append(element('p', `${group.identity?.platform || ''} ${group.identity?.subject || ''} · ${status === 'ok' ? '已更新' : '待确认'}`, 'group-meta'));
        const allocations = Array.isArray(group.allocations) ? group.allocations : [];
        const current = ['ok', 'fresh', 'active', 'ready', 'available', 'healthy'].includes(status);
        if (!current) panel.append(element('p', '此群数据可能过期或不可达；下方仅为服务返回的记录，空白不能视为没有占用。', 'notice warning'));
        if (!allocations.length) {
          panel.append(element('p', current ? '暂无资源记录。' : '当前没有可展示的资源记录，实际状态待恢复后确认。', 'empty'));
        } else {
          const scroll = element('div', undefined, 'table-scroll');
          const table = element('table');
          const head = element('thead');
          const row = element('tr');
          ['类型', '节点', '设备', '到期时间'].forEach(label => row.append(element('th', label)));
          head.append(row);
          const body = element('tbody');
          allocations.forEach(allocation => {
            const tr = element('tr');
            ['kind', 'node', 'device', 'expires_at'].forEach(key => tr.append(element('td', allocation[key] ?? '—')));
            body.append(tr);
          });
          table.append(head, body);
          scroll.append(table);
          panel.append(scroll);
        }
        app.append(panel);
      }
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
