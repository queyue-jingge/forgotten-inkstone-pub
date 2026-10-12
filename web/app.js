/* 讲课台 前端。纯轮询：拉 /api/state，变了就重绘。 */
(() => {
  const $ = (id) => document.getElementById(id);
  const courseSel = $('coursesel'), chapterSel = $('chaptersel'), liveBtn = $('livetoggle');
  const volWrap = $('volwrap');
  const lessonEl = $('lesson'), pendingEl = $('pending'), chatEl = $('chat');
  const imgEl = $('pageimg'), scrollEl = $('pdfscroll'), pdfHead = $('pdfhead');
  const pageBox = $('pagebox'), bookPage = $('bookpage');
  const statusDot = document.querySelector('#status .dot'), statusText = $('statustext');
  const inputEl = $('input'), sendBtn = $('send'), composer = $('composer');
  const railEl = $('rail');
  const boardEl = $('board'), boardSheet = $('boardsheet'), boardImgEl = $('boardimg');
  const boardToggle = $('boardtoggle'), boardOfferEl = $('boardoffer'), boardViewBtn = $('boardview');
  const boardFullBtn = $('boardfull'), boardOfferX = $('boardofferx');
  const boardResetBtn = $('boardreset');
  const bZoomOut = $('bzoomout'), bZoomIn = $('bzoomin'), bZoomVal = $('bzoomval');
  imgEl.draggable = false;
  boardImgEl.draggable = false;

  // 未登录/口令失效：任何接口回 401 就把整页踢回登录页
  const _fetch = window.fetch.bind(window);
  window.fetch = (input, init) => _fetch(input, init).then((r) => {
    if (r.status === 401) { location.href = '/login'; }
    return r;
  });

  let courses = {};

  // 顶部小提示（浮在右栏上方，几秒后自己隐）
  const hintEl = $('hint');
  let hintTimer = null;
  function showHint(msg) {
    if (!hintEl) return;
    hintEl.textContent = msg;
    hintEl.classList.add('on');
    clearTimeout(hintTimer);
    hintTimer = setTimeout(() => hintEl.classList.remove('on'), 4500);
  }
  let course = new URLSearchParams(location.search).get('course') || 'semi';
  let cur = null;              // 最近一次 state
  let lastMd = null, lastSrc = '', lastRev = -1;
  let pending = [];
  let inkOn = false, inkTool = 'pen';   // 标注模式（课本页上画/擦）
  let boardShown = false, boardOffer = false;   // 白板：显示中 / 有白砚开板待接受
  let boardFull = false;                        // 白板全屏（铺满整窗）
  let boardZoom = 1;                            // 全屏白板缩放（1=适屏）
  let boardSynced = false;                       // 首次拿到 state 时只对齐 ts、不弹"开板"问询（免得刷新次次冒）
  let boardTs = 0, boardClearRev = 0;           // 白砚"开板/清板"信号游标
  let boardWipedTs = -1;                         // 学生"还原"掉的题图 ts（纯白板；换题 ts 变则复位）
  let boardImgCur = null;                        // 当前白板底图 url
  let zoom = Math.min(4, Math.max(0.35,
    parseFloat(new URLSearchParams(location.search).get('zoom')) || 1)); // 1 = 适屏

  // ---------- 版面：左栏按课本比例，右栏吃掉剩下 ----------
  const stageEl = document.querySelector('.stage');
  const leftPane = document.querySelector('.pane.left');
  const headEl = pdfHead.parentElement;   // .pane-head
  const zoomFitBtn = $('zoomfit');
  const PAD = 20;             // .pdfscroll 上下留白
  const MIN_LEFT = 380;       // 左栏宽度地板(px)：保证 pane-head 的页码/缩放控件不被挤没
  function pageRatio() {
    const w = imgEl.naturalWidth, h = imgEl.naturalHeight;
    return (w && h) ? w / h : 0.707;   // 竖版书页默认比例
  }
  function layout() {
    // 用 pane 高度而非滚动框 clientHeight——后者会被页内滚动条改动，
    // 导致每次缩放左栏宽度被重算、中间边框跟着漂。
    const availH = Math.max(200, leftPane.clientHeight - headEl.offsetHeight - PAD);
    imgEl.style.height = Math.round(availH * zoom) + 'px';
    if (window.innerWidth > 900) {
      // 左栏宽度只认"适屏"那一档，放大也不跟着变宽（放大成在栏内滚动）
      const fitW = availH * pageRatio();
      const cap = stageEl.clientWidth * 0.72;
      // 给左栏一个地板：窄屏(平板竖过来)时别缩到 pane-head 控件挤成一团
      const leftW = Math.round(Math.min(Math.max(fitW + 34, MIN_LEFT), cap));
      stageEl.style.gridTemplateColumns = leftW + 'px minmax(0,1fr)';
    } else {
      stageEl.style.gridTemplateColumns = '';
    }
    scrollEl.classList.toggle('zoomed', zoom > 1.001);
    zoomFitBtn.textContent = Math.round(zoom * 100) + '%';
    sizeBoardSheet();
    placeInk('pdf'); placeInk('chat'); placeInk('board');   // 缩放/换页后把各层墨迹重新贴回内容
  }
  function setZoom(z) { zoom = Math.max(0.35, Math.min(4, z)); layout(); }

  // 放大后左键拖动平移
  let drag = null;
  scrollEl.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    if (inkOn) return;              // 标注模式下左键归笔/橡皮，不平移
    drag = { x: e.clientX, y: e.clientY, l: scrollEl.scrollLeft, t: scrollEl.scrollTop };
    scrollEl.classList.add('grabbing');
    e.preventDefault();
  });
  window.addEventListener('mousemove', (e) => {
    if (!drag) return;
    scrollEl.scrollLeft = drag.l - (e.clientX - drag.x);
    scrollEl.scrollTop = drag.t - (e.clientY - drag.y);
  });
  window.addEventListener('mouseup', () => {
    if (!drag) return;
    drag = null;
    scrollEl.classList.remove('grabbing');
  });
  $('zoomin').onclick = () => setZoom(zoom * 1.25);
  $('zoomout').onclick = () => setZoom(zoom / 1.25);
  zoomFitBtn.onclick = () => setZoom(1);
  scrollEl.addEventListener('wheel', (e) => {
    e.preventDefault();                       // 左栏滚轮 = 缩放；平移用左键拖
    setZoom(zoom * (e.deltaY < 0 ? 1.1 : 0.9));
  }, { passive: false });
  window.addEventListener('resize', layout);

  // ---------- markdown + katex ----------
  marked.setOptions({ breaks: true, gfm: true });

  function protectMath(md) {
    const store = [];
    const tok = (s) => { store.push(s); return '\u0000M' + (store.length - 1) + '\u0000'; };
    md = md.replace(/\$\$([\s\S]+?)\$\$/g, (m, g) => tok('$$' + g + '$$'));
    md = md.replace(/(?<!\\)\$([^\n$]+?)\$/g, (m, g) => tok('$' + g + '$'));
    return { md, store };
  }
  function restoreMath(html, store) {
    return html.replace(/\u0000M(\d+)\u0000/g, (m, i) => store[+i] || '');
  }
  function mdToHtml(md) {
    const { md: safe, store } = protectMath(md || '');
    return restoreMath(marked.parse(safe), store);
  }
  // 讲稿流：学生的发言用 <!--me--> ... <!--/me--> 包住，其余是分身正文
  function parseFlow(md) {
    const parts = [];
    const re = /<!--me-->([\s\S]*?)<!--\/me-->/g;
    let last = 0, m;
    while ((m = re.exec(md || ''))) {
      if (m.index > last) parts.push({ me: false, text: md.slice(last, m.index) });
      parts.push({ me: true, text: (m[1] || '').trim() });
      last = re.lastIndex;
    }
    if (last < (md || '').length) parts.push({ me: false, text: md.slice(last) });
    return parts;
  }
  // 取一句话开头几个字，当轨道悬浮标签
  function headOf(md) {
    const t = (md || '')
      .replace(/```[\s\S]*?```/g, ' ')
      .replace(/<svg[\s\S]*?<\/svg>/gi, ' ')
      .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
      .replace(/\$\$[\s\S]*?\$\$/g, ' ')
      .replace(/\$[^$]*\$/g, ' ')
      .replace(/[#>*_`\-\[\]()]/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
    return t.slice(0, 14);
  }

  let turns = [];        // 学生每一句（轮次轨的点，落在他发言处）
  let railDots = [];
  function renderMD(md) {
    const parts = parseFlow(md || '');
    lessonEl.innerHTML = '';
    turns = [];
    parts.forEach((p) => {
      if (p.me) {
        const d = document.createElement('div');
        d.className = 'round me';
        d.dataset.i = turns.length;
        d.innerHTML = mdToHtml(p.text);
        lessonEl.appendChild(d);
        turns.push({ el: d, head: headOf(p.text) || ('第 ' + (turns.length + 1) + ' 句') });
      } else if (p.text.trim()) {
        const d = document.createElement('div');
        d.className = 'round';
        d.innerHTML = mdToHtml(p.text);
        lessonEl.appendChild(d);
      }
    });
    try {
      renderMathInElement(lessonEl, {
        delimiters: [
          { left: '$$', right: '$$', display: true },
          { left: '$', right: '$', display: false },
          { left: '\\[', right: '\\]', display: true },
          { left: '\\(', right: '\\)', display: false },
        ],
        throwOnError: false,
      });
    } catch (e) { /* 公式坏了不影响正文 */ }
    buildRail();
    chatEl.scrollTop = chatEl.scrollHeight;
    placeInk('chat');       // 讲稿重排了，把讲稿栏墨迹层重新贴回内容上
  }

  // ---------- 轮次轨（点 = 学生的每句话）----------
  function buildRail() {
    railEl.innerHTML = '';
    railDots = [];
    if (turns.length < 1) { railEl.style.display = 'none'; return; }
    railEl.style.display = '';
    turns.forEach((r, i) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'rdot';
      b.dataset.i = i;
      b.title = r.head;
      b.innerHTML = '<span class="rlabel">' + escapeHtml(r.head) + '</span>';
      b.onclick = () => {
        // 只滚讲稿栏本身，绝不牵动外层（否则输入框跟着动）
        const y = r.el.getBoundingClientRect().top
                - chatEl.getBoundingClientRect().top + chatEl.scrollTop - 8;
        chatEl.scrollTo({ top: Math.max(0, y), behavior: 'smooth' });
      };
      railEl.appendChild(b);
      railDots.push(b);
    });
    setActiveRound(turns.length - 1);
  }
  function setActiveRound(i) {
    railDots.forEach((b, k) => b.classList.toggle('on', k === i));
  }
  let railTick = false;
  chatEl.addEventListener('scroll', () => {
    if (railTick || !turns.length) return;
    railTick = true;
    requestAnimationFrame(() => {
      railTick = false;
      const mark = chatEl.getBoundingClientRect().top + 44;
      let active = 0;
      turns.forEach((r, i) => { if (r.el.getBoundingClientRect().top <= mark) active = i; });
      setActiveRound(active);
    });
  });

  // ---------- 网页端输入的回显 ----------
  function renderPending() {
    pendingEl.innerHTML = pending
      .map((p) => {
        const imgs = (p.imgs || []).map((u) => '<img src="' + u + '">').join('');
        return '<div class="say">' + escapeHtml(p.text || '') +
          (imgs ? '<div class="pimgs">' + imgs + '</div>' : '') + '</div>';
      }).join('');
  }
  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // ---------- 页面图 ----------
  function showPage(vol, page) {
    const src = '/page/' + vol + '/' + page + '.jpg';
    if (src === lastSrc) return;
    lastSrc = src;
    scrollEl.classList.add('loading');
    imgEl.onload = () => { scrollEl.classList.remove('loading'); layout(); };
    imgEl.onerror = () => { scrollEl.classList.remove('loading'); };
    imgEl.src = src;
    scrollEl.scrollTop = 0; scrollEl.scrollLeft = 0;
  }

  // ---------- 顶栏：课程/章节两个下拉 + 唤醒开关 ----------
  function buildCourseSelect() {
    courseSel.innerHTML = '';
    Object.keys(courses).forEach((c) => {
      const o = document.createElement('option');
      o.value = c;
      o.textContent = courses[c].title;
      o.selected = c === course;
      courseSel.appendChild(o);
    });
  }
  courseSel.onchange = () => {
    course = courseSel.value;
    history.replaceState(null, '', '?course=' + course);
    lastMd = null; lastSrc = ''; lastRev = -1; pending = []; renderPending();
    lastChSig = ''; lastVolSig = '';
    poll();
  };

  let lastChSig = '', lastVolSig = '';
  function buildChapterSelect(st) {
    const chs = st.chapters || [];
    const sig = JSON.stringify(chs.map((c) => c.id)) + '|' + st.live + '|' + st.chapter;
    if (sig === lastChSig) return;
    lastChSig = sig;
    chapterSel.innerHTML = '';
    chs.forEach((ch) => {
      const o = document.createElement('option');
      o.value = ch.id;
      // 活着的那章名字前带个小亮点（●），切过去看/开下拉都能认出谁醒着
      o.textContent = (ch.id === st.live ? '● ' : '') + ch.id + '　' + ch.title;
      o.selected = ch.id === st.chapter;
      chapterSel.appendChild(o);
    });
  }
  chapterSel.onchange = () => {
    fetch('/api/chapter', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ course, chapter: chapterSel.value }),
    }).then(() => { lastMd = null; pending = []; renderPending(); lastChSig = ''; poll(); });
  };

  // 开关只管"当前看的这章活没活"；后台状态里记住哪章活着（st.live）
  function buildLiveBtn(st) {
    const on = !!st.live && st.live === st.chapter;
    liveBtn.classList.toggle('on', on);
    liveBtn.textContent = on ? '● 本节已唤醒' : '○ 唤醒本节';
    liveBtn.title = on ? '点一下：停用本节的会话' : '点一下：唤醒本节的会话';
  }
  liveBtn.onclick = () => {
    if (!cur) return;
    const on = !!cur.live && cur.live === cur.chapter;
    fetch('/api/live', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ course, value: on ? null : cur.chapter }),
    }).then((r) => r.json()).then((j) => {
      // 没开过的章：服务端不落 live，改成给控制端发了一单"开讲请求"
      if (j && j.queued) showHint('这章还没开过——已通知控制端，等确认再开');
      lastChSig = ''; poll();
    }).catch(() => {});
  };
  // 没唤醒的章：只能看，不能发
  function applyLock(st) {
    const on = !!st.live && st.live === st.chapter;
    composer.classList.toggle('locked', !on);
    inputEl.disabled = !on;
    sendBtn.disabled = !on;
    inputEl.placeholder = on
      ? '说点什么…（Enter 发送，Shift+Enter 换行；截图可直接粘贴或拖进来）'
      : '本节未唤醒——先点上方"唤醒本节"，就能对话了';
    // 本节没醒就没人接"读给白砚"——灰掉它
    inkReadBtn.disabled = !on;
    inkReadBtn.title = on ? '把本页连墨迹放进打字框' : '本节未唤醒，先唤醒本节再用';
  }

  function buildVolumes(st) {
    if (st.volumes.length <= 1) { volWrap.innerHTML = ''; lastVolSig = ''; return; }
    if (st.volume === lastVolSig) return;
    lastVolSig = st.volume;
    volWrap.innerHTML = '';
    const sel = document.createElement('select');
    st.volumes.forEach((v) => {
      const o = document.createElement('option');
      o.value = v;
      o.textContent = v === 'sig1' ? '上册' : v === 'sig2' ? '下册' : v;
      o.selected = v === st.volume;
      sel.appendChild(o);
    });
    sel.onchange = () => fetch('/api/volume', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ course, volume: sel.value }),
    }).then(poll);
    volWrap.appendChild(sel);
  }

  // ---------- 轮询 ----------
  async function poll() {
    try {
      const r = await fetch('/api/state?course=' + course, { cache: 'no-store' });
      const st = await r.json();
      if (st.error) throw new Error(st.error);
      statusDot.className = 'dot on'; statusText.textContent = '在线';
      cur = st;
      buildVolumes(st);
      buildChapterSelect(st);
      buildLiveBtn(st);
      applyLock(st);
      if (st.rev !== lastRev) {
        lastRev = st.rev;
        pageBox.value = st.page;
        bookPage.textContent = '书页 ' + st.book_page + ' / 共 ' + st.total_pages;
        pdfHead.textContent = st.volume_title;
        showPage(st.volume, st.page);
      }
      placeInk('pdf');        // 页面/卷若有变，课本页墨迹层跟着换页
      placeInk('board');
      // 白板：底图变→换图；白砚新开板→弹"选项"；白砚清板→抹掉本地墨迹
      const bd = st.board || {};
      const rect = (bd.x != null && bd.y != null && bd.w != null)
        ? { x: +bd.x, y: +bd.y, w: +bd.w } : null;
      if (rect) applyBoardRect(rect);        // 板不变，题图摆位可能被白砚改了
      const bts = bd.ts || 0;
      // 学生点过"还原"→ 把这一版的题图隐掉（纯白板）；白砚再推新题(ts 变)则照常显示
      const effImg = (bts === boardWipedTs) ? null : (bd.image || null);
      if (effImg !== boardImgCur) setBoardImage(effImg, rect);
      if (!boardSynced) { boardSynced = true; boardTs = bts; }   // 刷新首帧只对齐，不弹问询
      else if (bts !== boardTs) {
        boardTs = bts;
        if (bd.open && !boardShown) {          // 白砚新发起"想开板" → 给学生一个问询（点=开）
          boardOffer = true; boardOfferEl.hidden = false;
        }
      }
      const crv = bd.clear_rev || 0;
      if (crv !== boardClearRev) {
        boardClearRev = crv;
        if (INK.board.strokes.length) {
          INK.board.strokes = []; renderInk('board'); saveStrokes('board');
          showHint('白砚把白板擦了');
        }
      }
      if (st.markdown !== lastMd) {
        lastMd = st.markdown;
        renderMD(lastMd);
        if (pending.length) { pending = []; renderPending(); }
      }
    } catch (e) {
      statusDot.className = 'dot off'; statusText.textContent = '断线';
    }
  }

  // ---------- 翻页 ----------
  function goPage(p) {
    if (!cur) return;
    p = Math.max(1, Math.min(cur.total_pages, p));
    fetch('/api/page', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ course, page: p }),
    }).then(poll);
  }
  $('prev').onclick = () => goPage((cur ? cur.page : 1) - 1);
  $('next').onclick = () => goPage((cur ? cur.page : 1) + 1);
  pageBox.onchange = () => { const v = parseInt(pageBox.value, 10); if (v) goPage(v); };

  document.addEventListener('keydown', (e) => {
    if (e.target === inputEl) return;
    if (e.key === 'ArrowLeft') goPage((cur ? cur.page : 1) - 1);
    if (e.key === 'ArrowRight') goPage((cur ? cur.page : 1) + 1);
  });

  // ---------- 贴图 ----------
  const attachEl = $('attach');
  let images = [];              // [{dataUrl, mime}]
  function renderAttach() {
    attachEl.innerHTML = images.map((im, i) =>
      '<div class="thumb"><img src="' + im.dataUrl + '">' +
      '<button type="button" class="rm" data-i="' + i + '">×</button></div>').join('');
    attachEl.querySelectorAll('.rm').forEach((b) => {
      b.onclick = () => { images.splice(+b.dataset.i, 1); renderAttach(); };
    });
  }
  function addImageFile(f) {
    if (!f || !f.type.startsWith('image/')) return;
    const rd = new FileReader();
    rd.onload = () => { images.push({ dataUrl: rd.result, mime: f.type }); renderAttach(); };
    rd.readAsDataURL(f);
  }
  inputEl.addEventListener('paste', (e) => {
    const items = (e.clipboardData || {}).items || [];
    let hit = false;
    for (const it of items) {
      if (it.kind === 'file' && it.type.startsWith('image/')) { hit = true; addImageFile(it.getAsFile()); }
    }
    if (hit) e.preventDefault();
  });
  ['dragenter', 'dragover'].forEach((ev) => composer.addEventListener(ev, (e) => {
    if ([...(e.dataTransfer.types || [])].includes('Files')) { e.preventDefault(); composer.classList.add('drag'); }
  }));
  composer.addEventListener('dragleave', (e) => { if (e.target === composer) composer.classList.remove('drag'); });
  composer.addEventListener('drop', (e) => {
    e.preventDefault(); composer.classList.remove('drag');
    for (const f of (e.dataTransfer.files || [])) addImageFile(f);
  });

  // ---------- 发送 ----------
  composer.onsubmit = (e) => { e.preventDefault(); send(); };
  inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });
  inputEl.addEventListener('input', () => {
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(inputEl.scrollHeight, 144) + 'px';
  });
  async function send() {
    if (inputEl.disabled) return;
    const text = inputEl.value.trim();
    const imgs = images.slice();
    if (!text && !imgs.length) return;
    inputEl.value = ''; inputEl.style.height = 'auto';
    images = []; renderAttach();

    const urls = [];
    for (const im of imgs) {
      try {
        const r = await fetch('/api/upload', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ data: im.dataUrl, mime: im.mime }),
        });
        const j = await r.json();
        if (j.url) urls.push(j.url);
      } catch (e) { /* 单张失败不挡发送 */ }
    }
    pending.push({ text, imgs: imgs.map((i) => i.dataUrl) }); renderPending();
    chatEl.scrollTop = chatEl.scrollHeight;
    fetch('/api/say', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ course, text, images: urls }),
    }).catch(() => {});
  }

  // ---------- 标注：课本页 + 讲稿栏上的墨迹 ----------
  // 两层墨迹，各钉在自己内容的坐标上、各存各的：
  //   pdf  —— 钉页内"自然像素"（缩放跟着走、翻页各存；键 = 卷|PDF页）
  //   chat —— 钉讲稿内容像素（随讲稿滚动；键 = 课程|章）
  const inkToggle = $('inktoggle'), inkSeg = $('inkseg');
  const inkPenBtn = $('inkpen'), inkEraserBtn = $('inkeraser');
  const inkClearBtn = $('inkclear'), inkReadBtn = $('inkread');
  const SVGN = 'http://www.w3.org/2000/svg';
  const INK_COLOR = '#f87171';
  const INK = {
    pdf:  { svg: null, key: '', strokes: [], draw: null, drawEl: null, erase: false, vbw: 0, vbh: 0, r: 16, store: 'teach-ink:' },
    chat: { svg: null, key: '', strokes: [], draw: null, drawEl: null, erase: false, vbw: 0, vbh: 0, r: 14, store: 'teach-ink-chat:' },
    board:{ svg: null, key: '', strokes: [], draw: null, drawEl: null, erase: false, vbw: 0, vbh: 0, r: 16, store: 'teach-ink-board:' },
  };
  let inkLastPane = 'pdf';   // 最近落笔的那层（"清"就清它）

  function inkKeyOf(kind) {
    if (!cur) return '';
    if (kind === 'pdf') return cur.volume + '|' + cur.page;
    if (kind === 'board') return course + '|' + (cur.chapter || '') + '|board';
    return course + '|' + (cur.chapter || '');
  }
  // 该层的 SVG 该盖在哪块内容上（左上角 + 尺寸 + viewBox 自然尺寸）
  function inkGeom(kind) {
    if (kind === 'pdf') {
      if (!imgEl.naturalWidth || !imgEl.offsetWidth) return null;
      return { left: imgEl.offsetLeft, top: imgEl.offsetTop, w: imgEl.offsetWidth, h: imgEl.offsetHeight,
               vbw: imgEl.naturalWidth, vbh: imgEl.naturalHeight };
    }
    if (kind === 'board') {
      if (!boardShown || !boardSheet.offsetWidth || !boardSheet.offsetHeight) return null;
      const n = boardNatural();
      return { left: boardSheet.offsetLeft, top: boardSheet.offsetTop,
               w: boardSheet.offsetWidth, h: boardSheet.offsetHeight,
               vbw: n.w, vbh: n.h };      // 固定自然坐标系 → 缩放时墨迹跟着等比缩放
    }
    if (!lessonEl.offsetWidth || !lessonEl.offsetHeight) return null;
    return { left: lessonEl.offsetLeft, top: lessonEl.offsetTop, w: lessonEl.offsetWidth, h: lessonEl.offsetHeight,
             vbw: lessonEl.offsetWidth, vbh: lessonEl.offsetHeight };
  }
  function inkHost(kind) {
    return kind === 'pdf' ? scrollEl : (kind === 'board' ? boardEl : chatEl);
  }
  function penW(kind) {
    if (kind === 'pdf') return Math.max(3, Math.round((imgEl.naturalWidth || 1200) / 220));
    if (kind === 'board') return Math.max(3, Math.round(boardNatural().w / 220));
    return 3;
  }

  function ensureInkSvg(kind) {
    const L = INK[kind], host = inkHost(kind);
    if (L.svg && L.svg.parentNode === host) return L.svg;
    const s = document.createElementNS(SVGN, 'svg');
    s.setAttribute('class', 'inksvg' + (kind === 'chat' ? ' inkchat' : (kind === 'board' ? ' inkboard' : '')));
    s.addEventListener('pointerdown', (e) => onInkDown(kind, e));
    s.addEventListener('pointermove', (e) => onInkMove(kind, e));
    s.addEventListener('pointerup', (e) => onInkUp(kind, e));
    s.addEventListener('pointercancel', (e) => onInkUp(kind, e));
    host.appendChild(s);
    L.svg = s;
    return s;
  }

  function loadStrokes(kind, key) {
    try {
      const arr = JSON.parse(localStorage.getItem(INK[kind].store + key) || '[]');
      return Array.isArray(arr) ? arr : [];
    } catch (e) { return []; }
  }
  function saveStrokes(kind) {
    const L = INK[kind];
    if (!L.key) return;
    try {
      if (!L.strokes.length) localStorage.removeItem(L.store + L.key);
      else localStorage.setItem(L.store + L.key, JSON.stringify(L.strokes));
    } catch (e) { showHint('墨迹没存上（本地空间满？）'); }
  }

  // 把 SVG 层对齐到它的内容；键变了就载入对应墨迹
  function placeInk(kind) {
    const L = INK[kind], g = inkGeom(kind), key = inkKeyOf(kind);
    if (!g || !key) { if (L.svg) L.svg.style.display = 'none'; return; }
    const s = ensureInkSvg(kind);
    s.style.display = '';
    s.style.left = g.left + 'px';
    s.style.top = g.top + 'px';
    s.style.width = g.w + 'px';
    s.style.height = g.h + 'px';
    s.setAttribute('viewBox', '0 0 ' + g.vbw + ' ' + g.vbh);
    L.vbw = g.vbw; L.vbh = g.vbh;
    if (key !== L.key) {
      L.key = key;
      L.strokes = loadStrokes(kind, key);
      renderInk(kind);
    }
  }

  function renderInk(kind) {
    const L = INK[kind];
    if (!L.svg) return;
    while (L.svg.firstChild) L.svg.removeChild(L.svg.firstChild);
    L.strokes.forEach((s) => L.svg.appendChild(strokeEl(kind, s)));
    L.drawEl = null;
  }

  function strokeEl(kind, s) {
    const g = document.createElementNS(SVGN, 'g');
    const p = s.pts || [], w = s.w || penW(kind);
    if (p.length < 2) {
      const c = document.createElementNS(SVGN, 'circle');
      c.setAttribute('cx', p[0] ? p[0][0] : 0);
      c.setAttribute('cy', p[0] ? p[0][1] : 0);
      c.setAttribute('r', w / 2);
      c.setAttribute('fill', s.c || INK_COLOR);
      g.appendChild(c);
      return g;
    }
    const pl = document.createElementNS(SVGN, 'polyline');
    pl.setAttribute('points', p.map((q) => q[0] + ',' + q[1]).join(' '));
    pl.setAttribute('fill', 'none');
    pl.setAttribute('stroke', s.c || INK_COLOR);
    pl.setAttribute('stroke-width', w);
    pl.setAttribute('stroke-linecap', 'round');
    pl.setAttribute('stroke-linejoin', 'round');
    g.appendChild(pl);
    return g;
  }

  // 屏幕坐标 → 该层内容坐标
  function toLocal(kind, e) {
    const L = INK[kind];
    if (!L.svg || !L.vbw) return null;
    const r = L.svg.getBoundingClientRect();
    if (!r.width || !r.height) return null;
    const x = Math.max(0, Math.min(L.vbw, (e.clientX - r.left) / r.width * L.vbw));
    const y = Math.max(0, Math.min(L.vbh, (e.clientY - r.top) / r.height * L.vbh));
    return [Math.round(x), Math.round(y)];
  }

  function onInkDown(kind, e) {
    if (!inkOn || e.button !== 0) return;
    e.preventDefault();
    const L = INK[kind];
    if (L.svg.setPointerCapture) { try { L.svg.setPointerCapture(e.pointerId); } catch (x) {} }
    const r = L.svg.getBoundingClientRect();
    L.r = r.width ? Math.round(12 / r.width * L.vbw) : 14;
    inkLastPane = kind;
    if (inkTool === 'eraser') {
      L.erase = true;
      eraseAt(kind, toLocal(kind, e));
      return;
    }
    const p = toLocal(kind, e);
    if (!p) return;
    L.draw = { c: INK_COLOR, w: penW(kind), pts: [p] };
    L.strokes.push(L.draw);
    L.drawEl = strokeEl(kind, L.draw);
    L.svg.appendChild(L.drawEl);
  }
  function onInkMove(kind, e) {
    if (!inkOn) return;
    const L = INK[kind];
    const p = toLocal(kind, e);
    if (!p) return;
    if (L.erase) { eraseAt(kind, p); return; }
    if (!L.draw) return;
    const last = L.draw.pts[L.draw.pts.length - 1];
    if (Math.abs(p[0] - last[0]) + Math.abs(p[1] - last[1]) < 2) return;  // 抽点，别太密
    L.draw.pts.push(p);
    const el = strokeEl(kind, L.draw);
    if (L.drawEl && L.drawEl.parentNode === L.svg) L.svg.replaceChild(el, L.drawEl);
    else L.svg.appendChild(el);
    L.drawEl = el;
  }
  function onInkUp(kind) {
    const L = INK[kind];
    if (L.erase) { L.erase = false; saveStrokes(kind); return; }
    if (!L.draw) return;
    L.draw = null; L.drawEl = null;
    saveStrokes(kind);
  }
  function eraseAt(kind, p) {
    const L = INK[kind];
    if (!p) return;
    const r2 = L.r * L.r;
    const before = L.strokes.length;
    L.strokes = L.strokes.filter((s) =>
      !(s.pts || []).some((q) => {
        const dx = q[0] - p[0], dy = q[1] - p[1];
        return dx * dx + dy * dy <= r2;
      }));
    if (L.strokes.length !== before) { renderInk(kind); saveStrokes(kind); }
  }

  function setInkOn(v) {
    inkOn = !!v;
    inkToggle.classList.toggle('on', inkOn);
    scrollEl.classList.toggle('inkmode', inkOn);
    chatEl.classList.toggle('inkmode', inkOn);
    boardEl.classList.toggle('inkmode', inkOn);
    inkSeg.hidden = !inkOn;
    if (inkOn) { ensureInkSvg('pdf'); ensureInkSvg('chat'); ensureInkSvg('board'); }
    placeInk('pdf'); placeInk('chat'); placeInk('board');
  }
  function setInkTool(t) {
    inkTool = t;
    inkPenBtn.classList.toggle('on', t === 'pen');
    inkEraserBtn.classList.toggle('on', t === 'eraser');
  }
  inkToggle.onclick = () => setInkOn(!inkOn);
  inkPenBtn.onclick = () => setInkTool('pen');
  inkEraserBtn.onclick = () => setInkTool('eraser');
  inkClearBtn.onclick = () => {
    const L = INK[inkLastPane];
    if (!L.strokes.length) return;
    L.strokes = []; renderInk(inkLastPane); saveStrokes(inkLastPane);
    showHint(inkLastPane === 'pdf' ? '本页墨迹已清空'
            : (inkLastPane === 'board' ? '白板擦干净了' : '讲稿栏墨迹已清空'));
  };
  inkReadBtn.onclick = inkToComposer;

  // 「读给白砚」：把当前面（白板 或 课本页）+ 墨迹合成一张 JPEG，塞进打字框贴图区（不直接发），
  // 学生自己配文字、自己按发送。
  function inkToComposer() {
    if (!(cur && cur.live === cur.chapter)) {   // 本节没醒，打字框也锁着
      showHint('本节没在讲——先点上方「唤醒本节」，再读给白砚');
      return;
    }
    const onBoard = boardShown;
    const kind = onBoard ? 'board' : 'pdf';
    const cw = onBoard ? boardSheet.offsetWidth : imgEl.naturalWidth;
    const chh = onBoard ? boardSheet.offsetHeight : imgEl.naturalHeight;
    if (!cw || !chh) return;
    const cv = document.createElement('canvas');
    cv.width = cw; cv.height = chh;
    const g = cv.getContext('2d');
    g.fillStyle = '#fff'; g.fillRect(0, 0, cw, chh);
    if (onBoard && boardImgEl.naturalWidth) {
      const dw = cw * boardRect.w;                // 题图按白砚摆的位/大小贴上去，与屏上一致
      const dh = dw * boardImgEl.naturalHeight / boardImgEl.naturalWidth;
      g.drawImage(boardImgEl, cw * boardRect.x, chh * boardRect.y, dw, dh);
    } else if (!onBoard) {
      g.drawImage(imgEl, 0, 0, cw, chh);
    }
    g.strokeStyle = INK_COLOR; g.lineCap = 'round'; g.lineJoin = 'round';
    INK[kind].strokes.forEach((s) => {
      const p = s.pts || []; if (!p.length) return;
      g.lineWidth = s.w || 4;
      if (p.length === 1) {
        g.fillStyle = INK_COLOR;
        g.beginPath(); g.arc(p[0][0], p[0][1], (s.w || 4) / 2, 0, Math.PI * 2); g.fill();
        return;
      }
      g.beginPath();
      g.moveTo(p[0][0], p[0][1]);
      for (let i = 1; i < p.length; i++) g.lineTo(p[i][0], p[i][1]);
      g.stroke();
    });
    images.push({ dataUrl: cv.toDataURL('image/jpeg', 0.85), mime: 'image/jpeg' });
    renderAttach();
    if (boardFull) setBoardFull(false);   // 题读进打字框后退出全屏，好接着打字发送
    inputEl.focus();
    showHint(onBoard ? '白板已放进打字框——配句话再发' : '这页连墨迹已放进打字框——配句话再发');
  }

  // ---------- 白板：白砚开板/放题图，学生在板上手写（本地存） ----------
  // 板是一张固定的竖纸（A4 比例），**不随题图变**；题图由白砚用 x/y/w（0~1 小数）
  // 摆到板上（默认左上留白 6%、占宽 88%）。墨迹按板自然坐标存 → 换题图/全屏/缩放都不跑位。
  const BOARD_W = 1200, BOARD_H = 1697;
  let boardRect = { x: 0.06, y: 0.06, w: 0.88 };   // 当前题图在板上的位置/大小
  function boardNatural() { return { w: BOARD_W, h: BOARD_H }; }
  function sizeBoardSheet() {
    if (!boardShown) return;
    const availW = Math.max(40, boardEl.clientWidth - 20);
    const availH = Math.max(40, boardEl.clientHeight - 20);
    const n = boardNatural();
    let w = availW, h = w * n.h / n.w;
    if (h > availH) { h = availH; w = h * n.w / n.h; }
    boardSheet.style.width = Math.round(w * boardZoom) + 'px';
    boardSheet.style.height = Math.round(h * boardZoom) + 'px';
    boardEl.classList.toggle('zoomed', boardZoom > 1.001);
  }
  function updateBoardZoomLabel() { if (bZoomVal) bZoomVal.textContent = Math.round(boardZoom * 100) + '%'; }
  function setBoardZoom(z) {
    boardZoom = Math.max(0.35, Math.min(6, z));
    updateBoardZoomLabel();
    sizeBoardSheet();
    placeInk('board');
  }
  function showBoard(on) {
    boardShown = !!on;
    if (!boardShown) setBoardFull(false);
    boardEl.hidden = !boardShown;
    scrollEl.style.display = boardShown ? 'none' : '';
    boardToggle.classList.toggle('on', boardShown);
    boardFullBtn.style.display = boardShown ? '' : 'none';   // 全屏只在白板模式下出现
    boardResetBtn.style.display = boardShown ? '' : 'none';  // 还原也只在白板模式下出现
    if (boardShown) { boardOffer = false; boardOfferEl.hidden = true; }
    sizeBoardSheet();
    placeInk('board');
  }
  // 全屏：白板铺满整窗（顶栏留着——笔/擦/清/标注那些按钮都还在顶栏上，盖住就没法用了）
  const topbarEl = document.querySelector('.topbar');
  function setBoardFull(on) {
    boardFull = !!on;
    if (boardFull && !boardShown) showBoard(true);
    boardEl.classList.toggle('full', boardFull);
    boardEl.style.top = boardFull ? (topbarEl ? topbarEl.offsetHeight : 0) + 'px' : '';
    boardFullBtn.textContent = boardFull ? '⤢ 退出全屏' : '⛶ 全屏';   // 一键两态，不另设退出键
    boardZoom = 1; updateBoardZoomLabel();   // 进/出全屏都从适屏起
    sizeBoardSheet();
    placeInk('board');
  }
  function applyBoardRect(rect) {
    if (rect && rect.x != null && rect.y != null && rect.w != null) boardRect = rect;
    boardImgEl.style.left = (boardRect.x * 100) + '%';   // 相对板（boardsheet 有显式尺寸）
    boardImgEl.style.top = (boardRect.y * 100) + '%';
    boardImgEl.style.width = (boardRect.w * 100) + '%';
  }
  function setBoardImage(url, rect) {
    applyBoardRect(rect);
    boardImgCur = url || null;
    if (boardImgCur) {
      boardImgEl.onload = () => { sizeBoardSheet(); placeInk('board'); };
      boardImgEl.style.display = '';
      boardImgEl.src = boardImgCur;
    } else {
      boardImgEl.removeAttribute('src');
      boardImgEl.style.display = 'none';
    }
    sizeBoardSheet();
    placeInk('board');
  }
  boardToggle.onclick = () => showBoard(!boardShown);
  boardViewBtn.onclick = () => showBoard(true);                       // 接受白砚的"开板"问询
  boardOfferX.onclick = () => { boardOffer = false; boardOfferEl.hidden = true; };   // 先不开，关掉问询
  boardFullBtn.onclick = () => setBoardFull(!boardFull);   // 同一个键进出全屏（不再另设退出键）
  // 「还原」：把白板还原成纯白——清掉板上墨迹，并隐掉白砚这一版放的题图（各是独立于"清"的一键）
  boardResetBtn.onclick = () => {
    INK.board.strokes = []; renderInk('board'); saveStrokes('board');
    boardWipedTs = boardTs;              // 记下这一版 ts；白砚再推新题(ts 变)会重新显示
    setBoardImage(null, boardRect);
    showHint('白板已还原成纯白');
  };
  bZoomIn.onclick = () => setBoardZoom(boardZoom * 1.25);
  bZoomOut.onclick = () => setBoardZoom(boardZoom / 1.25);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && boardFull) { setBoardFull(false); }
  });
  // 全屏时：滚轮缩放、左键拖动平移（标注模式下左键归笔）
  boardEl.addEventListener('wheel', (e) => {
    if (!boardFull) return;
    e.preventDefault();
    setBoardZoom(boardZoom * (e.deltaY < 0 ? 1.1 : 0.9));
  }, { passive: false });
  let bdrag = null;
  boardEl.addEventListener('mousedown', (e) => {
    if (e.button !== 0 || !boardFull || inkOn) return;
    bdrag = { x: e.clientX, y: e.clientY, l: boardEl.scrollLeft, t: boardEl.scrollTop };
    boardEl.classList.add('panning');
    e.preventDefault();
  });
  window.addEventListener('mousemove', (e) => {
    if (!bdrag) return;
    boardEl.scrollLeft = bdrag.l - (e.clientX - bdrag.x);
    boardEl.scrollTop = bdrag.t - (e.clientY - bdrag.y);
  });
  window.addEventListener('mouseup', () => {
    if (!bdrag) return;
    bdrag = null; boardEl.classList.remove('panning');
  });
  boardFullBtn.style.display = 'none';   // 初始未开板 → 全屏键先藏着
  boardResetBtn.style.display = 'none';  // 还原键同
  updateBoardZoomLabel();

  // ---------- 启动 ----------
  fetch('/api/courses').then((r) => r.json()).then((cs) => {
    courses = cs;
    if (!courses[course]) course = Object.keys(courses)[0];
    buildCourseSelect();
    layout();
    poll();
    setInterval(poll, 1200);
  });
})();
