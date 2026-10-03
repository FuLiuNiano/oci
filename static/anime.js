/* 二次元主题特效：飘落花瓣 / 点击星星 / 光标拖尾 / 背景视差 / 主题切换过渡（纯本地，无外部依赖） */
(function () {
  const motion = matchMedia("(prefers-reduced-motion: reduce)");
  const disabled = () => motion.matches || document.documentElement.dataset.effects === "off";
  const reduce = false;
  const root = document.documentElement;
  document.querySelectorAll("[data-anime]").forEach(el=>el.removeAttribute("hidden"));

  /* ---- 花瓣 ---- */
  const cv = document.getElementById("petals");
  if (cv && !reduce) {
    const ctx = cv.getContext("2d");
    let W, H, dpr, petals = [], running = false;
    const COLORS = ["#ffb3d6", "#ff8ac0", "#ffd0e6", "#ffc4dd", "#e3d1ff"];
    function resize() {
      dpr = Math.min(devicePixelRatio || 1, 2);
      W = cv.width = innerWidth * dpr; H = cv.height = innerHeight * dpr;
    }
    function make(initial) {
      return {
        x: Math.random() * W, y: initial ? Math.random() * H : -30 * dpr,
        s: (7 + Math.random() * 9) * dpr, vy: (.35 + Math.random() * .8) * dpr, vx: (Math.random() - .3) * .5 * dpr,
        r: Math.random() * 6.28, vr: (Math.random() - .5) * .03, sw: Math.random() * 6.28, sws: .01 + Math.random() * .02,
        c: COLORS[(Math.random() * COLORS.length) | 0], a: .35 + Math.random() * .45
      };
    }
    function draw(p) {
      ctx.save();
      ctx.translate(p.x, p.y); ctx.rotate(p.r); ctx.scale(1, .55 + .45 * Math.cos(p.sw));
      ctx.globalAlpha = p.a; ctx.fillStyle = p.c;
      ctx.beginPath();
      const s = p.s;
      ctx.moveTo(0, -s);
      ctx.bezierCurveTo(s * .9, -s * .7, s * .8, s * .6, 0, s);
      ctx.bezierCurveTo(-s * .8, s * .6, -s * .9, -s * .7, 0, -s);
      ctx.fill();
      ctx.restore();
    }
    function tick() {
      if (!running) return;
      ctx.clearRect(0, 0, W, H);
      for (let i = 0; i < petals.length; i++) {
        const p = petals[i];
        p.sw += p.sws; p.r += p.vr;
        p.x += p.vx + Math.sin(p.sw) * .6 * dpr; p.y += p.vy;
        if (p.y > H + 30 * dpr || p.x > W + 40 * dpr || p.x < -40 * dpr) petals[i] = make(false);
        draw(petals[i]);
      }
      requestAnimationFrame(tick);
    }
    resize();
    petals = Array.from({ length: innerWidth < 700 ? 14 : 26 }, () => make(true));
    addEventListener("resize", resize);
    function resume() { const next = !document.hidden && !disabled(); if(next && !running) { running = true; requestAnimationFrame(tick); } else if(!next) running = false; }
    document.addEventListener("visibilitychange", resume);
    motion.addEventListener("change", resume);
    new MutationObserver(resume).observe(root, {attributes:true, attributeFilter:["data-effects"]});
    resume();
  }

  /* ---- 点击星星 ---- */
  const GLYPHS = ["✦", "✧", "★", "♥", "✿"];
  document.addEventListener("pointerdown", e => {
    if (disabled() || e.button !== 0 || e.target.closest("#term-area")) return;
    const hit = e.target.closest && e.target.closest("button, .chip, .session-card, .summary-card");
    const n = hit ? 8 : 4;
    for (let i = 0; i < n; i++) {
      const s = document.createElement("span");
      s.className = "spark";
      s.textContent = GLYPHS[(Math.random() * GLYPHS.length) | 0];
      const ang = Math.random() * 6.28, dist = 30 + Math.random() * 46;
      s.style.cssText = `left:${e.clientX}px;top:${e.clientY}px;--dx:${Math.cos(ang) * dist}px;--dy:${Math.sin(ang) * dist}px;--rot:${(Math.random() - .5) * 240}deg;` +
        `color:${["#ff6fa5", "#b79bff", "#6cc4ff", "#ffd36e"][i % 4]};font-size:${10 + Math.random() * 10}px`;
      document.body.appendChild(s);
      s.addEventListener("animationend", () => s.remove());
    }
  }, { passive: true });

  /* ---- 光标拖尾（节流） ---- */
  if (!reduce && matchMedia("(pointer: fine)").matches) {
    let last = 0;
    document.addEventListener("pointermove", e => {
      const t = performance.now();
      if (disabled() || e.target.closest("#term-area") || t - last < 55) return;
      last = t;
      const d = document.createElement("i");
      d.className = "trail";
      d.style.cssText = `left:${e.clientX}px;top:${e.clientY}px;transform:translate(-50%,-50%)`;
      document.body.appendChild(d);
      d.addEventListener("animationend", () => d.remove());
    }, { passive: true });

    /* 背景光斑视差 */
    const blobs = document.querySelectorAll(".bg-fx .blob");
    let raf = 0;
    document.addEventListener("pointermove", e => {
      if (disabled() || raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        const mx = e.clientX / innerWidth - .5, my = e.clientY / innerHeight - .5;
        blobs.forEach((b, i) => { b.style.translate = `${mx * (i + 1) * -30}px ${my * (i + 1) * -30}px`; });
      });
    }, { passive: true });
  }

  /* ---- 主题切换：全局颜色渐变 ---- */
  let themeTimer;
  new MutationObserver(() => {
    root.classList.add("theme-anim");
    clearTimeout(themeTimer);
    themeTimer = setTimeout(() => root.classList.remove("theme-anim"), 700);
  }).observe(root, { attributes: true, attributeFilter: ["data-theme"] });
})();

(() => {
  const root=document.documentElement;
  try { root.dataset.effects=localStorage.getItem('anime-effects') || 'on'; } catch { root.dataset.effects='on'; }
  const toggle=document.getElementById('anime-effects');
  function label(){toggle.textContent='特效：'+(root.dataset.effects==='off'?'关':'开');toggle.setAttribute('aria-pressed',String(root.dataset.effects!=='off'));}
  label();toggle.addEventListener('click',()=>{root.dataset.effects=root.dataset.effects==='off'?'on':'off';try{localStorage.setItem('anime-effects',root.dataset.effects);}catch{}label();});
  const lines=['今天也要记得保护好密钥哦。','慢慢来，我会在这里陪你。','云端的星星，今天也很明亮。','终端区域已经收起特效，专心操作吧。'];
  document.getElementById('character-talk').addEventListener('click',()=>{document.getElementById('character-bubble').textContent=lines[Math.floor(Math.random()*lines.length)];});
  const source=document.getElementById('toast'),dialog=document.getElementById('character-toast');
  function updateToast(){dialog.classList.toggle('show',source.classList.contains('show'));dialog.dataset.mood=source.classList.contains('err')?'sad':/中[.…]|稍后|等待/.test(source.textContent)?'thinking':'happy';document.getElementById('character-message').textContent=source.textContent;}
  new MutationObserver(updateToast).observe(source,{childList:true,characterData:true,subtree:true,attributes:true,attributeFilter:['class']});updateToast();
  const error=document.getElementById('login-err');
  new MutationObserver(()=>{if(!error.textContent)return;const card=document.querySelector('.login-card');card.classList.remove('character-error');void card.offsetWidth;card.classList.add('character-error');}).observe(error,{childList:true,characterData:true,subtree:true});
  document.querySelector('.login-card').addEventListener('animationend',e=>{if(e.animationName==='characterShake')e.currentTarget.classList.remove('character-error');});
  const pending=new Set();let timer=0;
  function annotate(){timer=0;for(const parent of pending){parent.querySelectorAll('.empty-state,[aria-busy="true"],.loading').forEach(el=>{if(!el.closest('#term-area'))el.classList.toggle('anime-loading',!el.classList.contains('empty-state'));});}pending.clear();}
  new MutationObserver(records=>{for(const r of records)if(r.addedNodes.length)pending.add(r.target.nodeType===1?r.target:r.target.parentElement);if(pending.size&&!timer)timer=requestAnimationFrame(annotate);}).observe(document.getElementById('app'),{subtree:true,childList:true});
})();
