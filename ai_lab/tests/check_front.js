// 前端真实 DOM 测试（jsdom）
// 两幕：
//   ① 喂真实抓下来的 SSE 流（按 7 字节切碎，模拟网络分包）→ 断言整条链路
//   ② 喂一段"模型被诱导输出 HTML"的流 → 断言转义生效，没被 XSS
//
// 跑法（需要 09_web_demo.py 正在 5001 上跑）：
//   NODE_PATH="C:/Users/dell/.workbuddy/binaries/node/workspace/node_modules" \
//   "C:/Users/dell/.workbuddy/binaries/node/versions/22.22.2-6/node.exe" tests/check_front.js
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const HERE = __dirname;
const BASE = 'http://127.0.0.1:5001';

// 把一段完整的 SSE 文本按 7 字节切碎 —— 真实网络里一条事件常被拆在两次 read 里
function makeBody(raw) {
  const bytes = new TextEncoder().encode(raw);
  const chunks = [];
  for (let i = 0; i < bytes.length; i += 7) chunks.push(bytes.slice(i, i + 7));
  let i = 0;
  return {
    getReader() {
      return {
        read() {
          if (i >= chunks.length) return Promise.resolve({ done: true, value: undefined });
          return Promise.resolve({ done: false, value: chunks[i++] });
        }
      };
    }
  };
}

async function main() {
  // 直接从服务端拿当前页面，而不是读一个可能过期的本地副本
  const res0 = await fetch(BASE + '/');
  if (!res0.ok) {
    console.error('拿不到页面，服务起来了吗？先跑 09_web_demo.py');
    process.exit(2);
  }
  const html = await res0.text();

  const dom = new JSDOM(html, { runScripts: 'dangerously', url: BASE + '/' });
  const w = dom.window;
  w.TextDecoder = TextDecoder;

  const q = s => w.document.querySelector(s);
  const all = s => [...w.document.querySelectorAll(s)];

  let pending = null;
  w.fetch = async (url) => {
    if (String(url).endsWith('/api/ask')) return { ok: true, status: 200, body: pending };
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  };

  async function waitDone(ms = 15000) {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
      if (q('.stat') || q('.err')) break;
      await new Promise(r => setTimeout(r, 40));
    }
    await new Promise(r => setTimeout(r, 120));
  }

  async function ask(rawStream, chipIndex = 0) {
    pending = makeBody(rawStream);
    const before = all('.msg').length;
    all('.chip')[chipIndex].dispatchEvent(new w.Event('click'));
    await waitDone();
    return before;
  }

  // ════════ 第一幕：真实流 ════════
  const rawReal = fs.readFileSync(path.join(HERE, 'sse_real.txt'), 'utf8');
  // 点第 4 个快捷问题 —— 它和 sse_real.txt 这段真实流是同一个问题，问和答能对上
  const before = await ask(rawReal, 3);

  const userBubble = all('.msg.user .bubble').pop();
  const botBubble = all('.msg.bot .bubble').pop();
  const tools = all('.tool');
  const stat = all('.stat').pop();

  console.log('── 第一幕：真实流（7 字节分包）──');
  console.log('① 用户气泡      :', !!userBubble, '| 长度', userBubble ? userBubble.textContent.length : 0);
  console.log('② AI 气泡       :', !!botBubble, '| 长度', botBubble ? botBubble.textContent.length : 0);
  console.log('③ 工具卡        :', tools.length, '张 |', tools.length ? tools[0].querySelector('.nm').textContent : '-');
  console.log('④ 统计行        :', stat ? stat.textContent : '-');
  console.log('⑤ 光标已撤      :', !botBubble.classList.contains('streaming'));
  console.log('⑥ 无残留 loading:', all('.status').length === 0);
  console.log('⑦ 无报错        :', all('.err').length === 0);
  console.log('⑧ 消息数        :', all('.msg').length, '应为', before + 2);

  const txt = botBubble.textContent;
  const mdOk = !txt.includes('**') && !!botBubble.querySelector('strong');
  console.log('⑨ Markdown 渲染 : ** 已消掉:', !txt.includes('**'),
              '| 有 <strong>:', !!botBubble.querySelector('strong'),
              '| 有 <code>:', !!botBubble.querySelector('code'));
  console.log('   正文前 80 字 :', txt.slice(0, 80).replace(/\n/g, ' '));

  const pass1 = !!userBubble && txt.length > 20 && tools.length > 0 && !!stat
    && !botBubble.classList.contains('streaming') && all('.err').length === 0
    && all('.msg').length === before + 2 && mdOk;

  // 存一份静态预览供截图（此刻第二幕还没跑，DOM 干净）
  [...w.document.querySelectorAll('script')].forEach(s => s.remove());
  fs.writeFileSync(path.join(HERE, '..', '_preview.html'),
                   '<!doctype html>\n' + w.document.documentElement.outerHTML);
  console.log('   → 已导出 _preview.html（截图用）');

  // ════════ 第二幕：恶意输出 ════════
  const evil = [
    'data: ' + JSON.stringify({ t: 'round', n: 1 }) + '\n\n',
    'data: ' + JSON.stringify({ t: 'delta', text: '好的，**这是粗体**，代码是 `scrypt`。' }) + '\n\n',
    'data: ' + JSON.stringify({ t: 'delta', text: '<img src=x onerror="window.__pwned=1">' }) + '\n\n',
    'data: ' + JSON.stringify({ t: 'delta', text: '<script>window.__pwned2=1</script>' }) + '\n\n',
    'data: ' + JSON.stringify({ t: 'done', status: 'ok', tokens: 10, elapsed: 0.1, rounds: 1, estimated: true }) + '\n\n',
  ].join('');

  await ask(evil, 0);

  const evilBubble = all('.msg.bot .bubble').pop();
  console.log('');
  console.log('── 第二幕：诱导模型输出 HTML ──');
  console.log('  气泡里有 <img> 吗 :', !!evilBubble.querySelector('img'));
  console.log('  气泡里有 <script>:', !!evilBubble.querySelector('script'));
  console.log('  window.__pwned    :', w.__pwned, '| __pwned2:', w.__pwned2);
  console.log('  原样显示成文字    :', evilBubble.textContent.includes('<img'));
  console.log('  粗体仍然生效      :', !!evilBubble.querySelector('strong'));

  const pass2 = !evilBubble.querySelector('img') && !evilBubble.querySelector('script')
    && !w.__pwned && !w.__pwned2
    && evilBubble.textContent.includes('<img')
    && !!evilBubble.querySelector('strong');

  console.log('');
  console.log(pass1 ? '✓ 第一幕通过' : '✗ 第一幕失败');
  console.log(pass2 ? '✓ 第二幕通过（转义生效，没被 XSS）' : '✗ 第二幕失败（有 XSS 风险）');
  process.exit(pass1 && pass2 ? 0 : 1);
}

main().catch(e => { console.error(e); process.exit(1); });
