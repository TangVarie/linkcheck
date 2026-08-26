"""面板的 HTML。单文件、内联样式和脚本、**零外部资源**。

没有 CDN、没有字体、没有图标库：一个内部监控面板不该因为某个 CDN 不通就
变成一张白纸，而且 CSP 里 `default-src 'none'` 本来就把外链全挡了。

排版的取舍：**待办列表放在最上面，项目卡片在下面。**
运营打开这个页面的问题是「现在有什么要我处理的」，不是「各项目的统计值」。
统计值是用来解释待办的，不是主角。

所有来自飞书的文本一律 `html.escape` 之后才进模板——那些字是运营和
陌生网友写的，直接拼进 HTML 就是存储型 XSS。
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any, Optional

from . import summary

_STYLE = """
/* BYWOOD 设计系统 v3 · 界面轨「管理后台」（scenarios/03-dashboard.md）
 *
 * 色值全部来自 tokens/palette.json 的 screen 节——它是全轨色板的唯一事实源，
 * 任何一处不一致按设计系统的规矩算**构建错误**，不是审美分歧。
 * 这里不引 tokens/globals.css：那是 Tailwind v4 的 @theme，而这个面板是
 * 零依赖的标准库 http.server 手拼 HTML，引不进来。所以从 palette.json
 * 取值、手写同名 CSS 变量，角色和取值都对齐。
 *
 * 硬性遵守的几条（DESIGN.md §5 §8 负面清单）：
 *   · 直角制度 border-radius: 0，例外只有本体即圆形的状态点
 *   · 零阴影、零渐变（骨架屏微光是明文例外）、零毛玻璃
 *   · 文字永不用纯黑纯白，只走四阶透明度；深色块内恒白是另一条规则
 *   · 语义色只表状态；浅底上的文字用加深档
 *   · 深色模式只翻 token，组件不写第二套颜色
 */
:root{
  --primary:#235E8E; --primary-dark:#1B4A72;
  --accent:#D9232B; --money:#D9232B;
  --block-blue:#235E8E; --block-red:#D9232B;
  --tint-sky:#E3EDF5; --tint-blush:#FBE3E3;
  --success:#00B578; --warning:#FF8F1F; --danger:#D92B3C;
  --success-deep:#006B4A; --warning-deep:#995400; --danger-deep:#B02330;
  --bg:#F3F4F6; --bg-secondary:#EAECEF; --surface:#FFFFFF;
  --fill:rgba(0,0,0,.045);
  --text-dark:rgba(0,0,0,.90); --text:rgba(0,0,0,.65);
  --text-light:rgba(0,0,0,.55); --text-faint:rgba(0,0,0,.30);
  --border:rgba(0,0,0,.08); --border-strong:#E5E6EB;
  --tint-danger:rgba(217,43,60,.10);
  --tint-warning:rgba(255,143,31,.10);
  --tint-success:rgba(0,181,120,.10);
  --font-sans:'PingFang SC','Hiragino Sans GB','Microsoft YaHei',-apple-system,
    BlinkMacSystemFont,'Segoe UI',sans-serif;
  --font-num:'DIN Alternate','Bahnschrift','Roboto Condensed','Helvetica Neue',
    var(--font-sans);
  color-scheme:light;
}
/* 深色模式：只翻 token。画布仍比表面深一档，深色块蓝红照旧白字。 */
[data-theme=dark]{
  --primary:#3F7FB2; --primary-dark:#346892;
  --accent:#E8575C; --money:#E8575C;
  --block-red:#C22329;
  --tint-sky:#1E3B54; --tint-blush:#442228;
  --success:#21B183; --warning:#FFA24D; --danger:#E8575C;
  --success-deep:#7FE0BD; --warning-deep:#FFC38A; --danger-deep:#FF9A9E;
  --bg:#0F1318; --bg-secondary:#151A20; --surface:#1B222B;
  --fill:rgba(255,255,255,.06);
  --text-dark:rgba(255,255,255,.92); --text:rgba(255,255,255,.70);
  --text-light:rgba(255,255,255,.55); --text-faint:rgba(255,255,255,.34);
  --border:rgba(255,255,255,.10); --border-strong:#2B3440;
  --tint-danger:rgba(232,87,92,.16);
  --tint-warning:rgba(255,162,77,.16);
  --tint-success:rgba(33,177,131,.16);
  color-scheme:dark;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme=light]){
    --primary:#3F7FB2; --primary-dark:#346892;
    --accent:#E8575C; --money:#E8575C;
    --block-red:#C22329;
    --tint-sky:#1E3B54; --tint-blush:#442228;
    --success:#21B183; --warning:#FFA24D; --danger:#E8575C;
    --success-deep:#7FE0BD; --warning-deep:#FFC38A; --danger-deep:#FF9A9E;
    --bg:#0F1318; --bg-secondary:#151A20; --surface:#1B222B;
    --fill:rgba(255,255,255,.06);
    --text-dark:rgba(255,255,255,.92); --text:rgba(255,255,255,.70);
    --text-light:rgba(255,255,255,.55); --text-faint:rgba(255,255,255,.34);
    --border:rgba(255,255,255,.10); --border-strong:#2B3440;
    --tint-danger:rgba(232,87,92,.16);
    --tint-warning:rgba(255,162,77,.16);
    --tint-success:rgba(33,177,131,.16);
    color-scheme:dark;
  }
}

*{box-sizing:border-box;border-radius:0}
body{margin:0;background:var(--bg);color:var(--text-dark);
  font:14px/1.6 var(--font-sans);-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
svg{display:block;flex:none}
.num{font-family:var(--font-num);font-variant-numeric:tabular-nums;
  letter-spacing:-.01em}
.muted{color:var(--text-light);font-size:12px}
.icon{width:16px;height:16px;stroke:currentColor;stroke-width:1.75;fill:none;
  stroke-linecap:round;stroke-linejoin:round;display:inline-block;
  vertical-align:-3px}

/* ---------- 骨架：侧栏 240 + 顶栏 56 + 内容区 24 / max 1440 ---------- */
.app{display:grid;grid-template-columns:240px minmax(0,1fr);min-height:100vh}
.side{background:var(--surface);border-right:1px solid var(--border);
  position:sticky;top:0;height:100vh;overflow:auto;display:flex;
  flex-direction:column}
.side .brand{height:64px;display:flex;align-items:center;gap:10px;
  padding:0 16px;border-bottom:1px solid var(--border);flex:none}
.side .brand b{font-size:15px;font-weight:700;color:var(--text-dark);
  letter-spacing:.02em}
.side .brand span{font-size:12px;color:var(--text-faint)}
.side nav{padding:8px 0 24px}
.side .grp{font-size:12px;color:var(--text-faint);padding:0 16px;
  margin:24px 0 6px}
.side nav a{display:flex;align-items:center;gap:10px;height:40px;padding:0 16px;
  font-size:14px;font-weight:500;color:var(--text);text-decoration:none}
.side nav a:hover{background:var(--fill);text-decoration:none}
/* 激活态是雾蓝底 + 品牌蓝字，不是深蓝底白字（03 场景点名的高发 bug） */
.side nav a.on{background:var(--tint-sky);color:var(--primary)}
.side nav a .cnt{margin-left:auto;font-size:12px;color:var(--text-faint)}
.side nav a.on .cnt{color:var(--primary)}
.side .foot{margin-top:auto;padding:16px;font-size:12px;color:var(--text-faint);
  border-top:1px solid var(--border)}
.top{height:56px;background:var(--surface);border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:12px;padding:0 24px;position:sticky;
  top:0;z-index:5}
.top .when{font-size:12px;color:var(--text-light);margin-left:auto;
  white-space:nowrap}
.content{padding:24px;max-width:1440px;margin:0 auto}

/* ---------- 标题与编辑式导语 ---------- */
h1{font-size:24px;font-weight:700;margin:0 0 6px;color:var(--text-dark)}
/* 导语：15px 基色 50%，重点词 90% + semibold（DESIGN.md §3） */
.lede{font-size:15px;color:var(--text-light);margin:0 0 24px;max-width:78ch}
.lede b{color:var(--text-dark);font-weight:600}
h2{font-size:16px;font-weight:600;margin:32px 0 4px;color:var(--text-dark);
  display:flex;align-items:baseline;gap:8px}
/* 侧栏走的是原生 hash 跳转，目标会被顶到视口最上沿——而 .top 是 56px 的
 * sticky，正好盖在那儿。点「余额」跳过去看到的会是余额区的第二行。
 * 56 顶栏 + 16 呼吸。 */
h2[id]{scroll-margin-top:72px}
/* 区块 / 卡片的入口链接。双箭挂这儿——BRAND.md §4 的「链接尾标」，
 * 也是 templates/dashboard.html 里双箭唯一出现的形态。 */
.enter{font-size:13px}
/* 品牌双箭：单个蓝色 »，字号同标题、字重 800（BRAND.md §4） */
h2 .n{font-weight:400;color:var(--text-faint)}
.sub{font-size:13px;color:var(--text-light);margin:0 0 12px;max-width:78ch}

/* ---------- KPI：1 个深蓝主块 + 最多 3 张白卡 ---------- */
.kpi{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;
  margin:0 0 8px}
.kpi>*{padding:16px 18px;min-height:112px;display:flex;flex-direction:column;
  justify-content:space-between}
.kpi .lead{background:var(--block-blue);color:#fff}
.kpi .lead .k{font-size:13px;color:rgba(255,255,255,.85)}
.kpi .lead .n{font-size:40px;font-weight:600;line-height:1.05}
.kpi .lead .s{font-size:12px;color:rgba(255,255,255,.85)}
.kpi .box{background:var(--surface);border:1px solid var(--border)}
.kpi .box .k{font-size:13px;color:var(--text-light)}
.kpi .box .n{font-size:32px;font-weight:600;line-height:1.1;color:var(--text-dark)}
.kpi .box .s{font-size:12px;color:var(--text-light)}
.kpi .box.warn .n{color:var(--danger-deep)}
.kpi .box .n small{font-size:15px;font-weight:500;color:var(--text-light);
  margin-left:4px}

/* ---------- 表格：表头 44 / 行 48 / 发丝线 / 无斑马纹 ---------- */
/* 表格自己横向滚动，不把整页撑宽。七列在手机上放不下是必然的，
 * 让页面横滚会把侧栏和顶栏一起推走。 */
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;background:var(--surface);
  border:1px solid var(--border)}
.tablewrap table{min-width:720px}
th{height:44px;background:var(--bg);font-size:13px;font-weight:500;
  color:var(--text-light);text-align:left;padding:0 12px;white-space:nowrap}
td{padding:12px;border-top:1px solid var(--border);font-size:14px;
  vertical-align:top;color:var(--text)}
tbody tr:hover{background:var(--fill)}
td.nowrap{white-space:nowrap}
td.right{text-align:right}
/* 行操作：13px 品牌蓝。scenarios/03 明写「行操作用 13px 蓝字链接」，
 * 而模板里通用的 a 是 accent 红——仲裁顺序中**场景文件规则高于模板**，
 * 这一处听场景的。 */
td a.act{font-size:13px;color:var(--primary)}
tr.fresh td:nth-child(2){border-left:3px solid var(--primary)}
tr.fresh td:nth-child(2)::after{content:"新";font-size:12px;color:var(--primary);
  margin-left:6px;font-weight:600}

/* ---------- 徽标：高 20、直角、语义浅底 + 加深档字 ---------- */
.chip{display:inline-block;height:20px;line-height:20px;padding:0 8px;
  font-size:12px;background:var(--fill);color:var(--text);
  margin:0 4px 4px 0;white-space:nowrap}
.chip.r{background:var(--tint-danger);color:var(--danger-deep)}
.chip.a{background:var(--tint-warning);color:var(--warning-deep)}
.chip.g{background:var(--tint-success);color:var(--success-deep)}

/* ---------- 卡片与色块 ---------- */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));
  gap:16px;align-items:start}
.card{background:var(--surface);border:1px solid var(--border);padding:18px}
.card.bad{border-color:var(--danger);border-left-width:3px}
.card h3{margin:0 0 2px;font-size:16px;font-weight:600;color:var(--text-dark)}
.card .rows{margin:12px 0 0;font-size:14px}
.card .rows div{display:flex;justify-content:space-between;gap:12px;
  padding:4px 0;border-top:1px solid var(--border)}
.card .rows div:first-child{border-top:0}
.card .rows .lbl{color:var(--text-light)}
.card .big{font-size:32px;font-weight:600;line-height:1.1;color:var(--text-dark)}

/* ---------- 横幅：浅调底 + 文字色阶（禁彩字配浅底） ---------- */
.problem,.note{padding:10px 12px;margin:12px 0;font-size:13px;
  display:flex;gap:8px;align-items:flex-start;line-height:1.55}
.problem{background:var(--tint-danger);color:var(--danger-deep);
  border-left:3px solid var(--danger)}
.note{background:var(--tint-warning);color:var(--warning-deep);
  border-left:3px solid var(--warning)}
.problem ul{margin:4px 0 0;padding-left:18px}
.problem li{margin:3px 0}
.problem .icon,.note .icon{margin-top:3px}

/* ---------- 控件：高 40 / 直角 / 聚焦 2px 品牌蓝环 ---------- */
button{font:600 14px/1 var(--font-sans);height:40px;padding:0 16px;border:0;
  background:var(--fill);color:var(--text-dark);cursor:pointer}
button:hover:not(:disabled){background:var(--bg-secondary)}
button:active:not(:disabled){transform:scale(.98)}
button:disabled{opacity:.45;cursor:not-allowed}
button.primary{background:var(--primary);color:#fff}
button.primary:hover:not(:disabled){background:var(--primary-dark)}
button.sm{height:32px;font-size:13px;font-weight:500;padding:0 12px}
button.danger{background:var(--danger);color:#fff}
input[type=password],input[type=search],input[type=text],input[type=number]{
  font:14px/1 var(--font-sans);height:40px;padding:0 12px;
  border:1px solid var(--border-strong);background:var(--surface);
  color:var(--text-dark);width:100%}
input:focus-visible,button:focus-visible,a:focus-visible,
summary:focus-visible{outline:2px solid var(--primary);outline-offset:1px}
:where(button,input,a){transition:background-color .12s ease-out,
  transform .12s ease-out}

/* ---------- 登录 ---------- */
.login{max-width:360px;margin:14vh auto;background:var(--surface);
  border:1px solid var(--border);padding:28px}
.login .brand{display:flex;align-items:center;gap:10px;margin-bottom:20px}
.login h1{font-size:20px;margin:0 0 4px}
.login p{margin:0 0 20px}
.login button{width:100%}
.err{color:var(--danger-deep);background:var(--tint-danger);padding:8px 10px;
  font-size:13px;margin:0 0 14px}

/* ---------- 四态：空 / 骨架 ---------- */
.empty{background:var(--surface);border:1px solid var(--border);padding:32px;
  text-align:center;color:var(--text-light);font-size:14px}
.empty .icon{width:24px;height:24px;stroke-width:1.5;margin:0 auto 10px;
  color:var(--text-faint)}
.empty .two{font-size:13px;color:var(--text-faint);margin-top:6px}
/* 骨架屏微光是负面清单 #2 明文豁免的唯一渐变 */
.sk{background:var(--bg-secondary);background-image:linear-gradient(90deg,
  rgba(0,0,0,0) 0%,var(--fill) 50%,rgba(0,0,0,0) 100%);
  background-size:200% 100%;animation:shimmer 1.6s linear infinite;height:14px}
.sk.tall{height:112px}
@keyframes shimmer{from{background-position:200% 0}to{background-position:-200% 0}}

/* ---------- 级联入场：动效必须有信息含义 ---------- */
.stagger-in>*{animation:fade-up .28s cubic-bezier(.22,1,.36,1) both}
.stagger-in>*:nth-child(2){animation-delay:.055s}
.stagger-in>*:nth-child(3){animation-delay:.11s}
.stagger-in>*:nth-child(4){animation-delay:.165s}
.stagger-in>*:nth-child(n+5){animation-delay:.22s}
@keyframes fade-up{from{opacity:0;transform:translateY(8px)}
  to{opacity:1;transform:none}}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.01ms!important;
    animation-iteration-count:1!important;transition-duration:.01ms!important}
}

/* ---------- 其余定式 ---------- */
.tools{display:flex;gap:10px;align-items:center}
.tools input{width:220px;height:36px}
.tools button{height:36px}
.queuebar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;
  margin-bottom:12px;background:var(--surface);border:1px solid var(--border);
  padding:12px}
.proj{display:flex;gap:12px;align-items:flex-start;padding:12px 0;
  border-top:1px solid var(--border);flex-wrap:wrap}
.proj:first-child{border-top:0}
.proj .who{flex:1;min-width:200px}
.proj .who b{font-weight:600;color:var(--text-dark)}
.proj .acts{display:flex;gap:8px}
.proj.off{opacity:.55}
.addbox{background:var(--surface);border:1px solid var(--border);padding:18px;
  margin-top:16px}
.addbox .row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.addbox input[type=text]{flex:1;min-width:200px}
.out{margin-top:12px;font-size:13px;white-space:pre-wrap;color:var(--text)}
.ok{color:var(--success-deep)}
code{background:var(--fill);padding:2px 6px;font-size:13px;
  font-family:var(--font-num)}
details>summary{cursor:pointer;font-size:13px;color:var(--text-light);
  padding:8px 0}
#projects{background:var(--surface);border:1px solid var(--border);padding:4px 18px}
@media (max-width:900px){
  .app{grid-template-columns:minmax(0,1fr)}
  .side{position:static;height:auto;border-right:0;
    border-bottom:1px solid var(--border)}
  .side nav{display:flex;flex-wrap:wrap;padding:8px}
  .side nav a{height:36px;padding:0 12px}
  .side .grp,.side .foot{display:none}
  .kpi{grid-template-columns:repeat(2,minmax(0,1fr))}
  .content{padding:16px}
  /* 顶栏在窄屏会溢出：固定 220px 的搜索框 + 按钮 + 时间戳 + 退出，
   * 加左右各 24px padding，375px 宽根本排不下，时间戳和退出会被挤出屏幕。
   * 搜索框改成可伸缩、时间戳藏起来（那个宽度下它不是决策信息）。 */
  .top{padding:0 16px;gap:8px}
  .tools{flex:1;min-width:0}
  .tools input{width:auto;flex:1;min-width:0}
  .top .when{display:none}
}
"""

# ⚠️ 这段脚本会**原样进到页面里**，所以连注释都不放 emoji——
# `tests/test_design.py` 的「不许拿 emoji 当图标」是按页面字节查的，
# 它不区分注释和正文，这样最严也最省事。Python 侧的注释不受影响。
_SCRIPT = """
(function(){
  var token = document.body.dataset.csrf || "";

  // 侧栏跟随滚动高亮。用 IntersectionObserver 触发，不挂 scroll 事件——
  // 后者每帧跑一次，长页面上白烧电池。动效有信息含义：它告诉你现在看的是哪一段。
  //
  // 注意：判据**不能**只看 isIntersecting。区块标题就 24px 高，一旦滚过顶部
  // 那条带子它就不再相交，于是「待办表格」那种长区块的绝大部分时间侧栏
  // 是没有高亮的。所以回调里按几何位置重算：取最后一个已经越过顶线的标题。
  // 区块只有五个，走一遍是 O(5)，比每帧跑一次便宜得多。
  var links = Array.prototype.slice.call(
    document.querySelectorAll(".side nav a[data-sec]"));
  if (links.length && window.IntersectionObserver) {
    var TOP = 72;                       // 顶栏 56 + 呼吸 16，和 scroll-margin 对齐
    function paint(){
      var active = "";
      links.forEach(function(a){
        var el = document.getElementById(a.dataset.sec);
        if (el && el.getBoundingClientRect().top <= TOP) active = a.dataset.sec;
      });
      // 一个都没越过顶线 = 还在页面最上面，highlight 第一个，别留空。
      if (!active) active = links[0].dataset.sec;
      links.forEach(function(a){
        a.classList.toggle("on", a.dataset.sec === active);
      });
    }
    var spy = new IntersectionObserver(paint, {
      rootMargin: "-" + TOP + "px 0px 0px 0px", threshold: [0, 1]});
    links.forEach(function(a){
      var el = document.getElementById(a.dataset.sec);
      if (el) spy.observe(el);
    });
    paint();
  }
  var btn = document.getElementById("refresh");
  if (btn) btn.addEventListener("click", function(){
    btn.disabled = true; btn.textContent = "取数中…";
    fetch("/api/refresh", {method:"POST", headers:{"X-Panel-Token": token}})
      .then(function(){ location.reload(); })
      .catch(function(){ btn.disabled = false; btn.textContent = "重新取数"; });
  });
  var box = document.getElementById("filter");
  if (box) box.addEventListener("input", function(){
    var q = box.value.trim().toLowerCase();
    var rows = document.querySelectorAll("#todos tbody tr");
    for (var i = 0; i < rows.length; i++) {
      var hit = !q || rows[i].textContent.toLowerCase().indexOf(q) >= 0;
      rows[i].style.display = hit ? "" : "none";
    }
  });

  // ---- 待办：批量勾「排队刷新」 + 标出自上次以来新出现的 ----
  var picks = document.querySelectorAll("#todos .todoPick");
  var btnQ = document.getElementById("btnQueue");
  var pickN = document.getElementById("pickN");
  var qOut = document.getElementById("queueOut");
  function rowsOf(el){ return el.closest("tr"); }
  function selected(){
    var out = [];
    for (var i = 0; i < picks.length; i++) {
      if (!picks[i].checked) continue;
      var tr = rowsOf(picks[i]);
      out.push({app_token: tr.dataset.app, table_id: tr.dataset.tbl,
                record_id: tr.dataset.rec});
    }
    return out;
  }
  function sync(){
    var n = selected().length;
    if (pickN) pickN.textContent = n;
    if (btnQ) btnQ.disabled = n === 0;
  }
  for (var i = 0; i < picks.length; i++) picks[i].addEventListener("change", sync);
  var all = document.getElementById("todoAll");
  if (all) all.addEventListener("change", function(){
    for (var i = 0; i < picks.length; i++) {
      var tr = rowsOf(picks[i]);
      if (tr.style.display !== "none") picks[i].checked = all.checked;
    }
    sync();
  });
  if (btnQ) btnQ.addEventListener("click", function(){
    var rows = selected();
    if (!rows.length) return;
    if (!confirm("给这 " + rows.length + " 行勾上「排队刷新」？\n\n" +
                 "面板不发付费请求——由后台 cron 在五分钟内接手，" +
                 "花费和你在飞书里手工勾完全一样。")) return;
    btnQ.disabled = true;
    if (qOut) qOut.textContent = "勾选中…";
    fetch("/api/queue", {method: "POST",
      headers: {"X-Panel-Token": token, "Content-Type": "application/json"},
      body: JSON.stringify({rows: rows})})
      .then(function(r){ return r.json().then(function(j){
        if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
        return j; }); })
      .then(function(j){
        if (qOut) qOut.textContent = "已勾 " + j.queued + " 行，等 cron 接手" +
          (j.failures.length ? "；" + j.failures.length + " 行失败：" + j.failures.join("；") : "");
        sync();
      })
      .catch(function(err){ if (qOut) qOut.textContent = String(err); btnQ.disabled = false; });
  });

  // 「问题发现」的关键是新增，不是总量。上次看到的存在浏览器本地，
  // 不上服务端——它是每个人自己的阅读进度，不是共享状态。
  try {
    var SEEN = "linkcheck.seen";
    var seen = JSON.parse(localStorage.getItem(SEEN) || "[]");
    var now = [], fresh = 0;
    var trs = document.querySelectorAll("#todos tbody tr");
    for (var i = 0; i < trs.length; i++) {
      var k = trs[i].dataset.key;
      if (!k) continue;
      now.push(k);
      if (seen.indexOf(k) < 0) { trs[i].className = "fresh"; fresh++; }
    }
    if (fresh && qOut) qOut.textContent = fresh + " 条是上次看过之后新出现的（标了「新」）";
    localStorage.setItem(SEEN, JSON.stringify(now.slice(0, 2000)));
  } catch (e) { /* 隐私模式/禁用存储：高亮没了而已，不影响别的 */ }

  // ---- 项目管理 ----
  var list = document.getElementById("projects");
  var out = document.getElementById("addOut");
  function esc(t){ var d = document.createElement("div"); d.textContent = t == null ? "" : t; return d.innerHTML; }
  function say(html, cls){ if (out) out.innerHTML = "<div class='" + (cls||"") + "'>" + html + "</div>"; }
  function post(action, body){
    return fetch("/api/projects/" + action, {
      method: "POST",
      headers: {"X-Panel-Token": token, "Content-Type": "application/json"},
      body: JSON.stringify(body || {})
    }).then(function(r){ return r.json().then(function(j){
      if (!r.ok) { throw new Error((j && (j.error || j.hint)) || ("HTTP " + r.status)); }
      return j; }); });
  }
  function thresholdForm(e){
    var cols = e.threshold_columns || [];
    var inputs = cols.map(function(c){
      var v = e.thresholds && e.thresholds[c] != null ? e.thresholds[c] : "";
      return "<label class=muted style='display:inline-block;margin:0 10px 6px 0'>" +
        esc(c) + " <input type=number min=1 style='width:90px' data-col='" + esc(c) +
        "' value='" + esc(String(v)) + "' placeholder='默认'></label>";
    }).join("");
    return "<div class=thr data-rec='" + esc(e.record_id) + "' hidden style='flex-basis:100%;margin-top:8px'>" +
      "<div class=muted style='margin-bottom:6px'>空着 = 用全局默认。" +
      "熔断比例、两击定罪、冷却、单轮预算<b>不能逐表</b>——它们有跨表语义，" +
      "逐表不同会让「这一轮该不该熔」说不清。</div>" +
      inputs +
      "<div><button type=button data-thr=preview>先算给我看</button> " +
      "<button type=button data-thr=save disabled>保存</button></div>" +
      "<div class='out thrOut'></div></div>";
  }
  function thrValues(box){
    var out = {};
    box.querySelectorAll("input[data-col]").forEach(function(i){
      out[i.dataset.col] = i.value.trim() === "" ? null : Number(i.value);
    });
    return out;
  }

  function load(){
    if (!list) return;
    fetch("/api/projects").then(function(r){ return r.json(); }).then(function(j){
      if (!j.enabled) { list.className = "note"; list.innerHTML = esc(j.hint); return; }
      if (j.error) { list.className = "problem"; list.innerHTML = esc(j.error); return; }
      if (!j.entries.length) { list.className = "empty"; list.textContent = "注册表还是空的，从下面加第一张。"; return; }
      list.className = "";
      list.innerHTML = j.entries.map(function(e){
        var bits = [];
        if (e.problem) bits.push("<span class='chip r'>" + esc(e.problem) + "</span>");
        else if (!e.enabled) bits.push("<span class=chip>已停用</span>");
        else bits.push("<span class='chip g'>巡查中</span>");
        return "<div class='proj" + (e.enabled ? "" : " off") + "'>" +
          "<div class=who><b>" + esc(e.label) + "</b> " + bits.join("") +
          "<div class=muted>" + esc(e.target) + "</div></div>" +
          "<div class=acts>" +
          "<button type=button data-act=build data-app='" + esc(e.app_token) + "' data-tbl='" + esc(e.table_id) + "'>补齐缺的列</button>" +
          "<button type=button data-act=enable data-rec='" + esc(e.record_id) + "' data-on='" + (e.enabled ? "0" : "1") + "'>" + (e.enabled ? "停用" : "启用") + "</button>" +
          "<button type=button data-act=thresholds data-rec='" + esc(e.record_id) + "'>阈值</button>" +
          "<button type=button data-act=remove data-rec='" + esc(e.record_id) + "' data-label='" + esc(e.label) + "'>移除</button>" +
          "</div>" + thresholdForm(e) + "</div>";
      }).join("");
    }).catch(function(err){ list.className = "problem"; list.textContent = String(err); });
  }
  if (list) {
    load();
    list.addEventListener("click", function(ev){
      var b = ev.target.closest ? ev.target.closest("button[data-act],button[data-thr]") : null;
      if (!b) return;
      var act = b.dataset.act;
      if (act === "thresholds") {
        var box = list.querySelector(".thr[data-rec='" + b.dataset.rec + "']");
        if (box) box.hidden = !box.hidden;
        return;
      }
      if (b.dataset.thr) {
        var panelBox = b.closest(".thr");
        var rec = panelBox.dataset.rec;
        var out2 = panelBox.querySelector(".thrOut");
        var saveBtn = panelBox.querySelector("[data-thr=save]");
        b.disabled = true;
        if (b.dataset.thr === "preview") {
          out2.textContent = "算一下…（不花钱）";
          post("thresholds", {record_id: rec, values: thrValues(panelBox), preview: true})
            .then(function(j){
              var lines = [j.describe];
              if (j.examples && j.examples.length) lines.push("比如：\n· " + j.examples.map(esc).join("\n· "));
              if (j.problems && j.problems.length) lines.push("这几处填得不对，已忽略：\n· " + j.problems.map(esc).join("\n· "));
              out2.innerHTML = lines.join("\n\n");
              saveBtn.disabled = false; b.disabled = false;
            })
            .catch(function(err){ out2.textContent = String(err); b.disabled = false; });
        } else {
          post("thresholds", {record_id: rec, values: thrValues(panelBox)})
            .then(function(){ out2.innerHTML = "<span class=ok>保存了。下一轮 cron（≤5 分钟）生效，不用重新部署。</span>"; })
            .catch(function(err){ out2.textContent = String(err); b.disabled = false; });
        }
        return;
      }
      if (act === "remove" && !confirm("不再监控「" + b.dataset.label + "」？\n\n只是从清单里去掉，飞书表和里面的数据一个字都不动。")) return;
      b.disabled = true;
      var body = act === "build" ? {app_token: b.dataset.app, table_id: b.dataset.tbl}
               : act === "enable" ? {record_id: b.dataset.rec, enabled: b.dataset.on === "1"}
               : {record_id: b.dataset.rec};
      post(act, body).then(function(j){
        if (act === "build") { say(esc(j.summary) + (j.skipped_options && j.skipped_options.length ? "\n\n这几处要去飞书手工补（补选项会整体覆盖，默认不代劳）：\n· " + j.skipped_options.map(esc).join("\n· ") : ""), j.ok ? "ok" : ""); }
        load();
      }).catch(function(err){ say(esc(String(err)), ""); b.disabled = false; });
    });
  }
  var btnCheck = document.getElementById("btnCheck");
  if (btnCheck) btnCheck.addEventListener("click", function(){
    var label = document.getElementById("addLabel").value.trim();
    var target = document.getElementById("addTarget").value.trim();
    if (!target) { say("先把表格链接粘进来", ""); return; }
    say("体检中…（不花钱）", "muted");
    post("check", {label: label, target: target}).then(function(j){
      var c = j.checkup, lines = [];
      if (c.duplicate) { say(esc(c.duplicate), ""); return; }
      if (!c.reachable) { say(esc(c.error), ""); return; }
      lines.push("读到了这张表" + (c.sample_rows === 0 ? "，但一行记录都没读到——多半也是没加协作者" : ""));
      if (c.buildable.length) lines.push("缺 " + c.buildable.length + " 列，面板能建：\n· " + c.buildable.map(esc).join("\n· "));
      if (c.manual.length) lines.push("这几条要人去飞书改（改类型会丢数据、补选项会整体覆盖，都不代劳）：\n· " + c.manual.map(esc).join("\n· "));
      if (c.ready) lines.push("配置齐了，可以直接入册。");
      lines.push("<button type=button id=btnAdd>加进清单</button> <span class=muted>加进来默认<b>不启用</b>，体检绿了再点启用</span>");
      say(lines.join("\n\n"), "");
      var add = document.getElementById("btnAdd");
      if (add) add.addEventListener("click", function(){
        add.disabled = true;
        post("add", {label: label, target: target, client_token: "add-" + target + "-" + label}).then(function(){
          say("加好了。<b>还没启用</b>——确认配置齐了再点上面那一行的「启用」。", "ok"); load();
        }).catch(function(err){ say(esc(String(err)), ""); add.disabled = false; });
      });
    }).catch(function(err){ say(esc(String(err)), ""); });
  });
  var btnCreate = document.getElementById("btnCreate");
  if (btnCreate) btnCreate.addEventListener("click", function(){
    var name = document.getElementById("addLabel").value.trim();
    if (!name) { say("先填个项目名", ""); return; }
    if (!confirm("新建一张监控表「" + name + "」？\n\n会在应用自己的空间里建一个多维表格，二十来列一次建齐。")) return;
    btnCreate.disabled = true;
    say("建表中…", "muted");
    post("create", {name: name}).then(function(j){
      var c = j.created;
      document.getElementById("addTarget").value = c.target;
      say("建好了：<a href='" + esc(c.url) + "' target=_blank rel='noopener noreferrer'>打开它 →</a>\n链接已经填进上面的输入框，点「体检一下」再入册。\n" + esc(c.note || ""), "ok");
      btnCreate.disabled = false;
    }).catch(function(err){ say(esc(String(err)), ""); btnCreate.disabled = false; });
  });
})();
"""

# 待办原因的配色。红 = 可能已经出事，黄 = 要跟进，其余中性。
_REASON_CLASS = {
    "风控中": "r", "已失效": "r", "刷新失败": "r",
    "有负面": "a", "疑似限流": "a", "疑似受限": "a",
    "卡住了": "a", "置顶掉了": "",
}


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


# lucide 线性图标，内联成 SVG。**不用 emoji 当图标**（负面清单 #1），
# 也不引外部图标库——这个面板是零依赖的，而且 CSP 是 default-src 'none'。
# 只放实际用到的几个，路径逐字取自 lucide 的 24×24 网格。
_ICON_PATHS = {
    "alert-triangle": ('<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 '
                       '4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/>'
                       '<path d="M12 17h.01"/>'),
    "alert-circle": ('<circle cx="12" cy="12" r="10"/><path d="M12 8v4"/>'
                     '<path d="M12 16h.01"/>'),
    "check-circle": ('<path d="M21.8 10A10 10 0 1 1 17 3.3"/>'
                     '<path d="m9 11 3 3L22 4"/>'),
    "inbox": ('<path d="M22 12h-6l-2 3h-4l-2-3H2"/>'
              '<path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6'
              'l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>'),
    "layout-grid": ('<rect width="7" height="7" x="3" y="3"/>'
                    '<rect width="7" height="7" x="14" y="3"/>'
                    '<rect width="7" height="7" x="14" y="14"/>'
                    '<rect width="7" height="7" x="3" y="14"/>'),
    "wallet": ('<path d="M19 7V4a1 1 0 0 0-1-1H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 1'
               'v4h-3a2 2 0 0 0 0 4h3a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1"/>'
               '<path d="M3 5v14a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1v-4"/>'),
    "folder": ('<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69'
               '-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>'),
    "history": ('<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>'
                '<path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>'),
}


def _icon(name: str, cls: str = "icon") -> str:
    body = _ICON_PATHS.get(name, "")
    return (f'<svg class="{cls}" viewBox="0 0 24 24" aria-hidden="true">'
            f'{body}</svg>')


# 品牌双箭。几何逐字沿用 logo/bywood-mark.svg：stroke 圆头圆角、
# 粗细 = 高度的 1/4（7/28）、固定朝右、固定蓝前红后（BRAND.md §4）。
# 这两个色值是标志本身的，不走界面 token —— 标志正红 #E5262B 只出现在
# 标志图形里，任何文字线条色块都不用这一档。
_MARK = (
    '<svg width="26" height="19" viewBox="0 0 38 28" role="img"'
    ' aria-label="BYWOOD"><path d="M5 4 L15 14 L5 24" fill="none"'
    ' stroke="#235E8E" stroke-width="7" stroke-linecap="round"'
    ' stroke-linejoin="round"/><path d="M21 4 L31 14 L21 24" fill="none"'
    ' stroke="#E5262B" stroke-width="7" stroke-linecap="round"'
    ' stroke-linejoin="round"/></svg>'
)


def _shell(title: str, body: str, *, csrf: str = "") -> str:
    return (
        "<!doctype html><html lang=zh-CN><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{_e(title)}</title><style>{_STYLE}</style></head>"
        f"<body data-csrf=\"{_e(csrf)}\">{body}"
        f"<script>{_SCRIPT}</script></body></html>"
    )


def login_page(message: str = "") -> str:
    error = f"<p class=err>{_e(message)}</p>" if message else ""
    return _shell("监控面板", f"""
<div class=login>
  <div class=brand>{_MARK}<b style='font-size:15px;letter-spacing:.02em'>BYWOOD</b></div>
  <h1>内容监控面板</h1>
  <p class=muted>小红书 / 抖音笔记巡检</p>
  {error}
  <form method=post action=/login>
    <input type=password name=password placeholder=口令 autofocus
           autocomplete=current-password>
    <p></p>
    <button type=submit class=primary>进去</button>
  </form>
</div>""")


def _stamp(ms: Optional[int], offset_hours: float = 8.0) -> str:
    """按显示时区渲染。和运行日志、和飞书表里那一格**逐字相同**——
    时间对不上是这个项目踩过的坑，面板不该把它再制造一遍。
    """
    if not ms:
        return "—"
    try:
        moment = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return "—"
    from datetime import timedelta
    tz = timezone(timedelta(hours=offset_hours))
    return moment.astimezone(tz).strftime("%m-%d %H:%M")


def _chips(values, mapping=None) -> str:
    out = []
    for value in values:
        cls = (mapping or {}).get(value, "")
        out.append(f"<span class='chip {cls}'>{_e(value)}</span>")
    return "".join(out) or "<span class=muted>—</span>"


def _counts_chips(counts: dict) -> str:
    if not counts:
        return "<span class=muted>—</span>"
    out = []
    for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        cls = _REASON_CLASS.get(name, "")
        out.append(f"<span class='chip {cls}'>{_e(name)} {count}</span>")
    return "".join(out)


def _todo_table(todos, offset_hours: float, show_digest: bool) -> str:
    if not todos:
        return ("<div class=empty>" + _icon("check-circle") +
                "没有需要处理的行。"
                "<div class=two>风控中 / 有负面 / 置顶掉了 / 已失效 / "
                "刷新失败 / 卡住了 —— 一条都没有。</div></div>")
    head = ("<tr><th><input type=checkbox id=todoAll title='全选'></th>"
            "<th>项目</th><th>为什么</th><th>诊断信息</th>"
            "<th>评论数</th><th>最近检查</th><th></th></tr>")
    body = []
    for todo in todos:
        extra = ""
        if show_digest and (todo.negative_digest or todo.digest):
            extra = (f"<div class=muted style='margin-top:4px'>"
                     f"{_e(todo.negative_digest or todo.digest)[:300]}</div>")
        body.append(
            f"<tr data-rec='{_e(todo.record_id)}' data-app='{_e(todo.app_token)}' "
            f"data-tbl='{_e(todo.table_id)}' data-key='{_e(todo.key)}'>"
            "<td class=nowrap><input type=checkbox class=todoPick></td>"
            f"<td class=nowrap>{_e(todo.project)}</td>"
            f"<td>{_chips(todo.reasons, _REASON_CLASS)}</td>"
            f"<td>{_e(todo.diagnosis) or '<span class=muted>—</span>'}{extra}</td>"
            f"<td class=nowrap>{_e(todo.comment_count) if todo.comment_count is not None else '—'}</td>"
            f"<td class=nowrap>{_e(_stamp(todo.checked_at_ms, offset_hours))}</td>"
            f"<td class=nowrap><a href='{_e(todo.record_url)}' "
            "target=_blank rel='noopener noreferrer' class=act>去这一行</a></td>"
            "</tr>")
    return (f"""<div class=queuebar>
  <button type=button id=btnQueue disabled>勾「排队刷新」（<span id=pickN>0</span>）</button>
  <span class=muted>面板不发付费请求——勾上之后由 cron 在五分钟内接手，
  和你在飞书里手工勾是同一条路。</span>
  <span id=queueOut class=muted></span>
</div>
<div class=tablewrap><table id=todos><thead>{head}</thead>"""
            f"<tbody>{''.join(body)}</tbody></table></div>")


def _archived_section(todos, offset_hours: float, show_digest: bool) -> str:
    """已归档但仍有异常的行。**折叠着，而且不在批量勾选的范围里。**

    「排队刷新」会绕过归档线——把这些老帖混进主列表，再配一个「全选」，
    就是一次把钱花在几个月前的内容上。它们大多数时候的正确处置是
    「取消巡查」，不是「重新刷新」。
    """
    if not todos:
        return ""
    rows = "".join(
        "<tr>"
        f"<td class=nowrap>{_e(t.project)}</td>"
        f"<td>{_chips(t.reasons, _REASON_CLASS)}</td>"
        f"<td>{_e(t.diagnosis) or '<span class=muted>—</span>'}</td>"
        f"<td class=nowrap><a href='{_e(t.record_url)}' target=_blank "
        "rel='noopener noreferrer' class=act>去这一行</a></td></tr>"
        for t in todos)
    return f"""<details style='margin-top:12px'>
  <summary class=muted style='cursor:pointer'>还有 {len(todos)} 行已归档的老帖也有异常
    （不在上面的列表和批量勾选里）</summary>
  <div class=note>{_icon('alert-triangle')}<span>这些帖子已经超过归档天数、
    不再自动刷了。「排队刷新」会<b>绕过归档线</b>，所以它们刻意不混进上面那一屏——
    多数时候正确的处置是去飞书<b>取消巡查</b>，而不是再花钱刷一次。</span></div>
  <div class=tablewrap><table>
  <thead><tr><th>项目</th><th>为什么</th><th>诊断信息</th><th></th></tr></thead>
  <tbody>{rows}</tbody></table></div>
</details>"""


def _project_card(p: summary.ProjectSnapshot, offset_hours: float) -> str:
    if p.error:
        return (f"<div class='card bad'><h3>{_e(p.label)}</h3>"
                f"<div class=problem>{_icon('alert-circle')}"
                f"<span>{_e(p.error)}</span></div>"
                f"<div class=rows><div><a href='{_e(p.table_url)}' target=_blank "
                "rel='noopener noreferrer' class=enter>打开这张表 »</a></div></div></div>")

    health = ""
    problems = list(p.health)
    if p.estimate_blocked:
        problems.insert(0, p.estimate_blocked)
    if problems:
        items = "".join(f"<li>{_e(problem)}</li>" for problem in problems)
        health = (f"<div class=problem>{_icon('alert-circle')}<span>"
                  f"<b>体检发现 {len(problems)} 个问题</b>"
                  f"<ul>{items}</ul></span></div>")

    due_text = (f"<span class=num>{p.due_rows} 行 ≈ ¥{p.due_yuan:.2f}</span>"
                if not p.estimate_blocked
                else "<b style='color:var(--danger-deep)'>无法估算</b>")
    seed_gap = p.total_rows - p.seed_keyword_rows
    neg_gap = p.total_rows - p.negative_keyword_rows
    coverage = (
        f"<div><span class=lbl>评论关键词</span><span>已填 {p.seed_keyword_rows}"
        f"/{p.total_rows}{f'（{seed_gap} 行没填）' if seed_gap else ''}</span></div>"
        f"<div><span class=lbl>负面词</span><span>已填 {p.negative_keyword_rows}"
        f"/{p.total_rows}{f'（{neg_gap} 行没填）' if neg_gap else ''}</span></div>")

    card_class = "card" if p.healthy else "card bad"
    # 截断了就必须说：把截断后的长度当成精确计数，等于在大面积事故的时候
    # 少报——而那正是最不该少报的时候。
    dropped = (f" <span class=muted>+{p.todos_dropped} 未显示</span>"
               if p.todos_dropped else "")
    return f"""<div class='{card_class}'>
  <h3>{_e(p.label)}</h3>
  <div class=muted><a href='{_e(p.table_url)}' target=_blank
     rel='noopener noreferrer' class=enter>打开这张表 »</a></div>
  {health}
  <div class=rows>
    <div><span class=lbl>在管</span><span>{p.total_rows} 行
      {f'<span class=muted>（{p.archived_rows} 已归档）</span>' if p.archived_rows else ''}</span></div>
    <div><span class=lbl>要人管</span><span>{p.needs_attention} 行{dropped}</span></div>
    <div><span class=lbl>到期待刷</span><span>{due_text}</span></div>
    <div><span class=lbl>排队中</span><span>{p.queued_rows} 行</span></div>
    <div><span class=lbl>卡住了</span><span>{p.stale_rows} 行
      {f'<span class=muted>（{p.never_checked_rows} 从没刷过）</span>' if p.never_checked_rows else ''}</span></div>
    <div><span class=lbl>最旧检查</span><span>{_e(_stamp(p.oldest_checked_ms, offset_hours))}</span></div>
    {coverage}
  </div>
  <div style='margin-top:9px'>{_counts_chips(p.traffic_tag_counts)}</div>
  <div style='margin-top:5px'>{_counts_chips(p.refresh_status_counts)}</div>
</div>"""


def _run_row(run, offset_hours: float) -> str:
    if not run.finished:
        badge = "<span class='chip r'>没跑完</span>"
    elif run.error:
        badge = f"<span class='chip r'>{_e(run.error)}</span>"
    elif run.breaker_tripped:
        badge = "<span class='chip r'>已熔断</span>"
    elif run.channels_dead:
        badge = "<span class='chip r'>通道全倒</span>"
    elif run.exit_code:
        badge = f"<span class='chip a'>退出码 {run.exit_code}</span>"
    elif run.stopped:
        badge = "<span class='chip a'>被终止</span>"
    else:
        badge = "<span class='chip g'>正常</span>"
    extra = []
    if run.failovers:
        extra.append(f"<span class='chip a'>降级 {run.failovers} 次</span>")
    if run.budget_stopped:
        extra.append("<span class='chip a'>预算触顶</span>")
    tables = "、".join(_e(t.label) for t in run.tables) or "—"
    # 「跑完还剩多少」——不是额外查的，每次付费响应本来就带 points.balance。
    # None = 这一轮没走 SocialDataX（全走 TikHub 了），显示「—」不是 ¥0。
    left = ("—" if run.points_balance is None
            else f"<span class=num>¥{run.points_yuan:.2f}</span>")
    started = _stamp(int(run.started_at * 1000) if run.started_at else None,
                     offset_hours)
    return ("<tr>"
            f"<td class=nowrap>{_e(started)}</td>"
            f"<td class=nowrap>{_e(run.mode or '—')}</td>"
            f"<td>{tables}</td>"
            f"<td class=nowrap>{run.rows}</td>"
            f"<td class='nowrap right num'>¥{run.cost_yuan:.2f}</td>"
            f"<td class='nowrap right'>{left}</td>"
            f"<td>{badge}{''.join(extra)}</td>"
            "</tr>")


def _runs_section(runs, log_error: str, offset_hours: float) -> str:
    note = (f"<div class=note>{_icon('alert-triangle')}"
            f"<span>取 Railway 日志失败：{_e(log_error)}</span></div>"
            if log_error else "")
    if not runs:
        body = ("<div class=empty>" + _icon("inbox") +
                "暂时没有可解析的运行记录。"
                "<div class=two>需要 cron 那个服务设上 "
                "<code>RUN_LOG_JSON=1</code>，日志才带结构化事件；"
                "Railway 免费 / Hobby 套餐日志只留 7 天。</div></div>")
        return f"<h2 id=s-runs>运行历史</h2>{note}{body}"
    head = ("<tr><th>开跑</th><th>模式</th><th>表</th><th>行数</th>"
            "<th>花费</th><th>跑完剩</th><th>结果</th></tr>")
    rows = "".join(_run_row(r, offset_hours) for r in runs)
    unfinished = sum(1 for r in runs if not r.finished)
    warn = ""
    if unfinished:
        warn = (f"<div class=note>{_icon('alert-triangle')}<span>"
                f"有 {unfinished} 轮只有开跑没有收尾——"
                "那是被容器杀掉的形态（redeploy、回收、OOM），"
                "那几轮已经付过钱的结果多半丢了。</span></div>")
    return (f"<h2 id=s-runs>运行历史 <span class=n>{len(runs)}</span></h2>{note}{warn}"
            f"<div class=tablewrap><table><thead>{head}</thead>"
            f"<tbody>{rows}</tbody></table></div>")


def _projects_section(config) -> str:
    """项目管理。**内容由前端异步拉** —— 它要读注册表，
    而注册表和聚合缓存是两条独立的路：注册表读不到不该让整个面板变空白。
    """
    patch = ("自动补选项：<b>已开</b>" if getattr(config, "allow_option_patch", False)
             else "自动补选项：<b>关</b>（缺选项只给清单，"
                  "在废表上验过再开 <code>PANEL_ALLOW_OPTION_PATCH=1</code>）")
    return f"""<h2 id=s-manage>项目</h2>
<div class=muted style='margin-bottom:8px'>加表、停用、移除都在这儿。
「移除」只是不再监控，<b>不动你飞书表里的任何数据</b>。{patch}</div>
<div id=projects><div class=sk style='margin:14px 0;width:70%'></div><div class=sk style='margin:14px 0;width:55%'></div><div class=sk style='margin:14px 0;width:62%'></div></div>
<div class=addbox>
  <div style='font-weight:600;margin-bottom:8px'>加一张表</div>
  <div class=row>
    <input type=text id=addLabel placeholder='项目名，比如 途鸽三期'>
    <input type=text id=addTarget placeholder='粘表格链接（/base/ 或 /wiki/ 都行）'>
    <button type=button id=btnCheck>体检一下</button>
  </div>
  <div class=muted>体检不花钱。也可以 <button type=button id=btnCreate>直接新建一张</button>
    —— 二十来列连类型带选项一次建齐，一次飞书都不用点。</div>
  <div id=addOut class=out></div>
</div>"""


def _balance_section(balances, balance_error: str, runway, config) -> str:
    """余额那一块。

    ⚠️ **读不到就说读不到，绝不显示 ¥0。** 真的余额为 0 和读不到余额，
    要人做的事完全相反（一个去充值，一个去查 Key），而 ¥0 长得像前者。
    这和「缺列时 estimate 报 ¥0.00」是同一类错误。
    """
    if not balances and not balance_error:
        return ""
    if not balances:
        return (f"<h2 id=s-balance>余额</h2><div class=empty>"
                + _icon("wallet") + f"{_e(balance_error)}</div>")

    warn_days = getattr(config, "runway_warn_days", 14.0)
    alert_days = getattr(config, "runway_alert_days", 5.0)
    cards = []
    for b in balances:
        if b.error:
            cards.append(
                f"<div class='card bad'><h3>{_e(b.label)}</h3>"
                f"<div class=problem>{_icon('alert-circle')}"
                f"<span>读不到：{_e(b.error)}</span></div>"
                "<div class=muted>这不等于余额为 0——去对应后台看一眼，"
                "或检查 Key 是不是过期了</div></div>")
            continue
        if b.unit == "USD":
            main = f"${b.amount:.2f}"
            sub = (f"≈ ¥{b.yuan:.2f}（按 {b.rate:g} 折算）"
                   if b.yuan is not None else "")
            sub_cls = "muted num"
        else:
            main = f"{b.amount:.0f} 积分"
            sub = f"≈ ¥{b.yuan:.2f}" if b.yuan is not None else ""
            sub_cls = "muted num"
        credit = (f"<div class=muted>另有赠送额度 {b.free_credit:g}</div>"
                  if b.free_credit else "")
        cards.append(
            f"<div class=card><h3>{_e(b.label)}</h3>"
            f"<div class='big num'>{_e(main)}</div>"
            f"<div class='{sub_cls}'>{_e(sub)}</div>{credit}</div>")

    note = ""
    if runway is not None and runway.known:
        cls = "problem" if runway.days <= alert_days else (
            "note" if runway.days <= warn_days else "muted")
        partial = ("；<b>只算了读得到的那家</b>，实际比这个多"
                   if runway.partial else "")
        note = (f"<div class={cls}>按最近 {runway.runs_used} 轮"
                f"（{runway.hours_covered:.1f} 小时）的实际花速 "
                f"<span class=num>¥{runway.yuan_per_day:.2f}/天</span>，余额还够跑 "
                f"<b class=num>{runway.days:.0f} 天</b>{partial}</div>")
    elif runway is not None and runway.reason:
        note = f"<div class=muted>还算不出「够跑多久」：{_e(runway.reason)}</div>"
    err = (f"<div class=note>{_icon('alert-triangle')}<span>"
           f"取余额失败：{_e(balance_error)}，"
           "下面是上一次还能用的读数。</span></div>" if balance_error else "")
    return (f"<h2 id=s-balance>余额</h2>{err}{note}"
            f"<div class=grid>{''.join(cards)}</div>"
            "<div class=muted style='margin-top:8px'>这两个查询端点是两家"
            "<b>官方标明零费用</b>的（TikHub 计价表 <code>endpoint_cost: 0.0</code>；"
            "SocialDataX <code>x-socialdatax-cost-points: 0</code>）—— "
            "看余额本身不花钱。</div>")


def overview_page(*, overview: Optional[summary.Overview], error: str,
                  fetched_at: float, config, csrf: str = "",
                  runs=None, log_error: str = "",
                  balances=None, balance_error: str = "", runway=None,
                  offset_hours: float = 8.0) -> str:
    if overview is None:
        # 四态里的「骨架加载」。给骨架而不是一句「正在取数」，是因为这一屏
        # 要读完整张表，第一次可能要好几秒——一个空盒子看着像坏了。
        boxes = "".join("<div class='sk tall'></div>" for _ in range(4))
        lines = "".join(
            f"<div class=sk style='margin:10px 0;width:{w}%'></div>"
            for w in (92, 78, 85, 60))
        problem = (f"<div class=problem>{_icon('alert-circle')}"
                   f"<span>{_e(error)}</span></div>" if error else "")
        inner = f"""<div class=app>
<aside class=side>
  <div class=brand>{_MARK}<b>BYWOOD</b><span>监控</span></div>
</aside>
<div class=main>
  <div class=top><span class=when>正在第一次取数…</span></div>
  <div class=content>
    <h1>内容监控面板</h1>
    <p class=lede>正在把每张表读一遍，第一次要几秒钟。这一步<b>不花钱</b>——
      飞书读表不计费。</p>
    {problem}
    <div class=kpi>{boxes}</div>
    <div style='margin-top:24px'>{lines}</div>
  </div>
</div>
</div>"""
        return _shell("监控面板", inner, csrf=csrf)

    todos = overview.todos()
    # 每张表内部按 max_todos 截掉的 + 跨表拉平之后再截掉的。两个都要算，
    # 否则「要人管 200 行」在一次大面积事故里会被当成精确值。
    todos_hidden = overview.todos_dropped + overview.todos_dropped_by()
    hidden_note = (f" 另有 {todos_hidden} 行同样需要处理，"
                   "因为条数上限没显示——先处理完这一屏再回来看。"
                   if todos_hidden else "")
    # 零个项目要说清楚是「还没加」而不是「都健康」。全零的一屏和
    # 「一切正常」长得一模一样，这是这套东西最难发现的那类故障。
    empty_note = ("" if overview.projects else
                  f"<div class=note>{_icon('alert-triangle')}<span>"
                  "注册表里还没有一张可用的表——"
                  "在下面「项目」那一栏加第一张。加完等一轮缓存（或点"
                  "「重新取数」）就会出现在这里。</span></div>")
    projects = overview.projects
    stale_note = ""
    if error:
        stale_note = (f"<div class=note>{_icon('alert-triangle')}<span>"
                      f"上一次取数失败（{_e(error)}），"
                      "下面是上一份还能用的快照。</span></div>")
    domain_note = ""
    if config.feishu_base.rstrip("/") == "https://feishu.cn":
        domain_note = (f"<div class=note>{_icon('alert-triangle')}<span>"
                       "没设 <code>FEISHU_DOMAIN</code>，"
                       "「去这一行」的链接用的是通用域名，不一定跳得准。"
                       "把你们租户的域名（形如 <code>https://xxx.feishu.cn</code>）"
                       "设进去就好了。</span></div>")

    # KPI 行的组成是**设计系统定死的**：1 个深蓝主块 + 最多 3 张白卡
    # （DESIGN.md 负面清单 #9、scenarios/03）。上一版是 6–7 个同规格方块
    # 排排坐，正好是那条禁令点名的形态——数字一样多，但没有主次，
    # 眼睛落不到「今天该干什么」上。
    #
    # 主块给「要人管」：那是这个面板存在的理由。其余按「要不要现在做决定」
    # 挑三个：待刷多少钱（下一轮的支出）、还够跑多久（要不要充值）、
    # 卡住了多少（机器是不是没在干活）。在管行数、排队中、项目数这些
    # 属于背景信息，降到主块下面那行 meta 里。
    lead_sub = (f"另有 {todos_hidden} 行未显示" if todos_hidden
                else ("按严重度排序" if todos else "这一屏是干净的"))
    cost = f"¥{overview.due_yuan:.2f}"
    cost_sub = (f"{len(overview.unestimatable)} 张表算不出，这是下界"
                if overview.unestimatable else f"{overview.due_rows} 行到期待刷")
    kpi = [
        _kpi_lead(f"{len(todos)}" + ("+" if todos_hidden else ""),
                  "要人管", lead_sub),
        _kpi_box(cost + ("+" if overview.unestimatable else ""),
                 "下一轮预计花费", cost_sub),
        *_runway_box(runway, config),
        _kpi_box(overview.stale_rows, "卡住了",
                 "早该刷到却一直没轮到" if overview.stale_rows
                 else "没有积压", warn=bool(overview.stale_rows)),
    ]
    bar = "".join(kpi[:4])

    # 编辑式导语：一行，15px 基色 50%，重点词 90% + semibold。
    # 后台轨把它收敛成一行放在页面标题下方（scenarios/03「文字」）。
    # 它替代了上一版顶栏那一排读不出主次的数字。
    # 截断的时候导语也不能按精确值说。它和侧栏计数是页面上最显眼的两处，
    # 大面积事故时在这两处少报，正是最不该少报的时候。
    # 导语是句子，有地方把话讲清楚——不像 KPI 只能挂一个 `+`。
    todo_text = (f"<b>{len(todos)}</b> 行现在需要人看一眼"
                 f"（另有 <b>{todos_hidden}</b> 行因条数上限没列出来）"
                 if todos_hidden else
                 f"<b>{len(todos)}</b> 行现在需要人看一眼")
    lede = (f"在管 <b>{overview.total_rows}</b> 行，分在 "
            f"<b>{len(projects)}</b> 个项目里；其中 {todo_text}，"
            f"<b>{overview.queued_rows}</b> 行已排队等下一轮刷新。")

    nav = _sidebar_nav(
        # 侧栏那个数字和 KPI 走同一个口径：截断了就带 `+`，不装成精确值。
        todos=f"{len(todos)}" + ("+" if todos_hidden else ""),
        projects=len(projects), runs=len(runs or []),
        # 余额整块读失败时 `_balance_section` 仍然渲染一个 id=s-balance 的
        # 错误区块——侧栏这边也得有入口，否则「余额出事了」这个信息恰好在
        # 它最该出现的时候从导航里消失。
        balances=bool(balances) or bool(balance_error))
    when = _stamp(int(fetched_at * 1000) if fetched_at else None, offset_hours)

    inner = f"""<div class=app>
<aside class=side>
  <div class=brand>{_MARK}<b>BYWOOD</b><span>监控</span></div>
  {nav}
  <div class=foot>面板只读飞书表，<b>不发任何付费请求</b>。<br>
    要刷新某一行，勾「排队刷新」，cron 五分钟内接手。</div>
</aside>
<div class=main>
  <div class=top>
    <span class=tools>
      <input id=filter type=search placeholder='筛待办…' aria-label='筛待办'>
      <button id=refresh type=button class=sm>重新取数</button>
    </span>
    <span class=when>数据于 {_e(when)} 取得</span>
    <form method=post action=/logout style='margin:0 0 0 12px'>
      <button type=submit class=sm>退出</button></form>
  </div>
  <div class=content>
    <h1>内容监控面板</h1>
    <p class=lede>{lede}</p>
    {stale_note}{domain_note}
    <div class='kpi stagger-in'>{bar}</div>

    <h2 id=s-todo>要人管的行 <span class=n>{len(todos)}{'+' if todos_hidden else ''}</span></h2>
    <p class=sub>跨所有项目拉平，按严重度排序。点「去这一行」直接落到飞书表里那一行——
      面板负责发现，具体处理在飞书里做。{hidden_note}</p>
    {_todo_table(todos, offset_hours, config.show_digest)}
    {_archived_section(overview.archived_todos(), offset_hours, config.show_digest)}

    <h2 id=s-proj>各项目 <span class=n>{len(projects)}</span></h2>
    {empty_note}<div class=grid>{''.join(_project_card(p, offset_hours) for p in projects)}</div>

    {_projects_section(config)}

    {_balance_section(balances or [], balance_error, runway, config)}

    {_runs_section(runs or [], log_error, offset_hours)}
  </div>
</div>
</div>"""
    return _shell("监控面板", inner, csrf=csrf)


def _sidebar_nav(*, todos, projects: int, runs: int,
                 balances: bool) -> str:
    """侧栏导航。导航项高 40、图标 20 + 文字 14/500，激活 = 雾蓝底 + 蓝字。

    带上每一项的条数：侧栏在这个面板里不只是跳转，它是**第二处**告诉你
    「哪儿有事」的地方——收着的时候也能一眼看出待办有多少。
    """
    items = [
        ("s-todo", "alert-circle", "要人管的行", str(todos)),   # 可能带 `+`
        ("s-proj", "layout-grid", "各项目", str(projects)),
        ("s-manage", "folder", "加表 / 改配置", ""),
    ]
    if balances:
        items.append(("s-balance", "wallet", "余额", ""))
    items.append(("s-runs", "history", "运行历史", str(runs) if runs else ""))
    links = "".join(
        f"<a href='#{anchor}' data-sec='{anchor}'>"
        f"{_icon(icon, 'icon')}<span>{_e(label)}</span>"
        + (f"<span class=cnt>{_e(count)}</span>" if count else "")
        + "</a>"
        for anchor, icon, label, count in items)
    return f"<nav>{links}</nav>"


def _kpi_lead(value, key: str, sub: str = "") -> str:
    """KPI 行的深蓝主块。**一屏只有一个**，给最该被看见的那个数。

    块内文字恒白（DESIGN.md §2「深色块（蓝/红）恒白」），不用文字色阶
    token —— 那套是给画布上的文字的，放进深底会看不清。
    """
    return (f"<div class=lead><div class=k>{_e(key)}</div>"
            f"<div class='n num'>{_e(value)}</div>"
            f"<div class=s>{_e(sub)}</div></div>")


def _kpi_box(value, key: str, sub: str = "", *, warn: bool = False,
             unit: str = "", sub_money: bool = False) -> str:
    """白 surface KPI 卡。**最多三张**，且必须配一个主块。

    `unit` 单独一个参数而不是让调用方把 `<small>` 拼进 value —— value 要
    转义，混进去的标签会被转义成字面量。
    """
    # 数字和中文量词之间**留一个空格**：模板里一律 `3 天` / `20 条` / `6 个`。
    tail = f"<small> {_e(unit)}</small>" if unit else ""
    # `sub` 里带金额时那一行也要 num（负面清单 #12 管的是**金额数字**，
    # 不分它在主位还是说明位）。sub 是要转义的纯文本，所以靠这个开关
    # 给容器加类，而不是让调用方往里拼标签。
    sub_cls = "s num" if sub_money else "s"
    return (f"<div class='box{' warn' if warn else ''}'>"
            f"<div class=k>{_e(key)}</div>"
            f"<div class='n num'>{_e(value)}{tail}</div>"
            f"<div class='{sub_cls}'>{_e(sub)}</div></div>")


def _runway_box(runway, config) -> list:
    """「还够跑几天」那一格。算不出就**整格不显示**——
    放一个「—」只会占掉主块旁边三个位置里的一个，还让人以为是 0。
    """
    if runway is None or not runway.known:
        return []
    alert_days = getattr(config, "runway_alert_days", 5.0)
    warn_days = getattr(config, "runway_warn_days", 14.0)
    sub = f"按 ¥{runway.yuan_per_day:.2f}/天 的实际花速"
    if runway.partial:
        sub += "，有一家读不到，这是下界"
    return [_kpi_box(f"{runway.days:.0f}", "余额还够跑", sub, unit="天",
                     sub_money=True,
                     warn=runway.days <= max(alert_days, warn_days))]
