/* ============================================================
   第 10 步 · 对话页前端
   ------------------------------------------------------------
   和第 9 步的差别只有三处：
     ① 历史消息是服务端渲染好的，这里只补一遍 Markdown 格式化
     ② 一问一答都带着 sid，服务端才有地方落库
     ③ 答完之后顺手把标题刷新一下（服务端按第一句话改了名）

   其余全部沿用第 9 步打磨过的那套：SSE 分帧、retract、工具卡片。
   ============================================================ */
(function () {
  'use strict';

  const chat   = document.getElementById('chat');
  const stream = document.getElementById('stream');
  const box    = document.getElementById('q');
  const btn    = document.getElementById('send');
  const SID    = window.SID;
  let busy = false;
  let firstAsk = !document.querySelector('.msg.user');

  /* ── 渲染工具 ── */
  function el(cls, text) {
    const d = document.createElement('div');
    d.className = cls;
    if (text !== undefined) d.textContent = text;
    return d;
  }

  /* 只有当用户本来就在底部附近才自动跟到底。
     不然他往上翻着看历史，新字一进来就被拽下来，很烦。 */
  function nearBottom() {
    return stream.scrollHeight - stream.scrollTop - stream.clientHeight < 160;
  }
  function scrollDown(force) {
    if (force || nearBottom()) stream.scrollTop = stream.scrollHeight;
  }

  /* ── 极简 Markdown ──
     模型输出是不可信内容：它完全可能吐出 <img src=x onerror=...>，
     直接 innerHTML 就等于开了一个 XSS 口子。
     所以顺序必须是：先转义 → 再替换标记。反过来就中招。 */
  function escHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function renderMd(text) {
    let h = escHtml(text);
    h = h.replace(/^#{1,6}\s+(.+)$/gm, '<strong>$1</strong>');
    h = h.replace(/^\s*-{3,}\s*$/gm, '<span class="hr"></span>');
    h = h.replace(/\*\*([^*\n]+?)\*\*/g, '<strong>$1</strong>');
    h = h.replace(/`([^`\n]+?)`/g, '<code>$1</code>');
    return h;
  }

  /* 服务端渲染的历史气泡里是原文，这里统一格式化一次。
     不用 innerHTML 读原值 —— 用 textContent，避免把已经转义过的
     &amp; 再转一遍变成 &amp;amp;。 */
  document.querySelectorAll('[data-md]').forEach(b => {
    b.innerHTML = renderMd(b.textContent);
  });
  scrollDown(true);

  /* ── 造节点 ── */
  function addUser(text) {
    const m = el('msg user');
    m.appendChild(el('avatar', '我'));
    m.appendChild(el('bubble', text));
    chat.appendChild(m);
    scrollDown(true);
  }

  function addBot() {
    const m = el('msg bot');
    m.appendChild(el('avatar', 'AI'));
    const col = el('col');
    const bubble = el('bubble streaming');
    col.appendChild(bubble);
    m.appendChild(col);
    chat.appendChild(m);
    scrollDown(true);
    return { msg: m, col: col, bubble: bubble };
  }

  /* 工具卡片挂在这个回答的 .col 里，排在气泡前面。
     因为 .col 是纵向 flex，appendChild 会落在气泡下面，
     所以用 insertBefore(bubble) 插到前面。 */
  function addTool(ev, ui) {
    const t = el('tool');
    const r1 = el('row1');
    const nm = el('nm', ev.name);
    const ar = el('ar', ev.args || '{}');
    r1.appendChild(nm);
    r1.appendChild(ar);
    t.appendChild(r1);
    t.appendChild(el('br', '→ ' + ev.brief));
    ui.col.insertBefore(t, ui.bubble);
    scrollDown();
  }

  function addStatus(text, ui) {
    clearStatus();
    const s = el('status');
    s.appendChild(el('spin'));
    s.appendChild(el('span', text));
    s.dataset.tmp = '1';
    ui.col.insertBefore(s, ui.bubble);
    scrollDown();
  }

  function clearStatus() {
    chat.querySelectorAll('.status[data-tmp]').forEach(n => n.remove());
  }

  /* ── 事件处理 ── */
  const STATUS_CN = {
    timeout: '超时', max_rounds: '到达轮次上限',
    aborted: '中途中断', error: '出错', empty: '没有返回内容'
  };

  function handle(ev, ui) {
    switch (ev.t) {
      case 'round':
        clearStatus();
        if (ev.n > 1) addStatus('第 ' + ev.n + ' 轮：再想想…', ui);
        break;

      case 'status':
        addStatus(ev.text, ui);
        break;

      case 'delta':
        ui.bubble.textContent += ev.text;
        scrollDown();
        break;

      case 'retract':
        /* 本轮推出去的字只是"我先查一下"这类话术，留着会被当成答案 */
        ui.bubble.textContent = '';
        break;

      case 'tool':
        clearStatus();
        addTool(ev, ui);
        break;

      case 'done': {
        clearStatus();
        ui.bubble.classList.remove('streaming');
        /* 流式时只能当纯文本追加（半截 ** 没法渲染），收完再整体格式化 */
        ui.bubble.innerHTML = renderMd(ui.bubble.textContent);

        const parts = [];
        if (ev.status && ev.status !== 'ok') {
          parts.push('⚠ ' + (STATUS_CN[ev.status] || ev.status));
        }
        parts.push(ev.elapsed + ' 秒');
        parts.push(ev.rounds + ' 轮');
        if (ev.tokens) {
          parts.push((ev.estimated ? '约 ' : '') + ev.tokens + ' tokens' +
                     (ev.estimated ? '（估算）' : ''));
        }
        const stat = el('stat', parts.join(' · '));
        ui.col.appendChild(stat);
        scrollDown();

        /* 第一句问完，服务端已经拿它当标题了。把顶栏和侧栏同步一下，
           不然用户会觉得"我起了名字它没理我"。 */
        if (firstAsk) {
          firstAsk = false;
          const t = (lastQuestion || '').replace(/\s+/g, ' ').slice(0, 24);
          const ht = document.querySelector('.topbar .ht');
          if (ht && t) {
            ht.textContent = t;
            const item = document.querySelector('.sitem.on .st');
            if (item) item.textContent = t;
          }
        }
        break;
      }

      case 'error':
        clearStatus();
        ui.bubble.classList.remove('streaming');
        ui.bubble.classList.add('err');
        ui.bubble.textContent = '出错了：' + ev.message;
        break;
    }
  }

  /* ── 主流程 ── */
  let lastQuestion = '';

  async function ask(text) {
    if (busy) return;
    busy = true;
    btn.disabled = true;
    lastQuestion = text;

    const intro = document.getElementById('intro');
    if (intro) intro.remove();

    addUser(text);
    const ui = addBot();

    try {
      const res = await fetch('/api/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ q: text, sid: SID })
      });

      if (!res.ok) {
        let msg = 'HTTP ' + res.status;
        try { msg = (await res.json()).error || msg; } catch (e) {}
        throw new Error(msg);
      }

      const reader = res.body.getReader();
      const dec = new TextDecoder('utf-8');
      let buf = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });

        /* SSE 以空行分帧。一条事件可能被拆在两次 read 里，
           必须缓冲到凑齐 "\n\n" 再处理 —— 手写 SSE 最容易错的地方。 */
        let i;
        while ((i = buf.indexOf('\n\n')) >= 0) {
          const frame = buf.slice(0, i);
          buf = buf.slice(i + 2);
          for (const line of frame.split('\n')) {
            if (!line.startsWith('data: ')) continue;
            let ev;
            try { ev = JSON.parse(line.slice(6)); } catch (e) { continue; }
            handle(ev, ui);
          }
        }
      }
    } catch (e) {
      ui.bubble.classList.remove('streaming');
      ui.bubble.classList.add('err');
      ui.bubble.textContent = '连接失败：' + e.message;
    } finally {
      ui.bubble.classList.remove('streaming');
      clearStatus();
      busy = false;
      btn.disabled = false;
      box.focus();
    }
  }

  function submit() {
    const t = box.value.trim();
    if (!t || busy) return;
    box.value = '';
    box.style.height = 'auto';
    ask(t);
  }

  /* ── 绑定 ── */
  btn.addEventListener('click', submit);

  box.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); }
  });

  box.addEventListener('input', () => {
    box.style.height = 'auto';
    box.style.height = Math.min(box.scrollHeight, 140) + 'px';
  });

  document.querySelectorAll('.chip').forEach(c => {
    c.addEventListener('click', () => { box.value = c.textContent; submit(); });
  });

  document.querySelectorAll('.intro .card').forEach(c => {
    c.addEventListener('click', () => { box.value = c.dataset.q; submit(); });
  });

  const del = document.getElementById('delBtn');
  if (del) {
    del.addEventListener('click', async () => {
      if (!confirm('删除这个对话？消息会一起删掉，不能恢复。')) return;
      const res = await fetch('/api/session/' + SID, { method: 'DELETE' });
      if (res.ok) location.href = '/new';
      else alert('删除失败');
    });
  }

  box.focus();
})();
