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
:root{--bg:#f6f7f9;--card:#fff;--line:#e3e6ea;--ink:#1c1f23;--dim:#6b7280;
--red:#c0392b;--amber:#b7791f;--green:#1e7d44;--blue:#1d4ed8;--chip:#eef1f5}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",
"Helvetica Neue",Arial,sans-serif}
a{color:var(--blue)}
.wrap{max-width:1180px;margin:0 auto;padding:20px 16px 60px}
header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:14px}
h1{font-size:19px;margin:0}
.muted{color:var(--dim);font-size:12px}
.bar{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
gap:10px;margin:14px 0 22px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:12px 14px}
.stat .n{font-size:24px;font-weight:600;letter-spacing:-.02em}
.stat .k{font-size:12px;color:var(--dim);margin-top:2px}
.stat.alert .n{color:var(--red)}
h2{font-size:15px;margin:26px 0 10px;display:flex;align-items:center;gap:8px}
table{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);
vertical-align:top;font-size:13px}
th{background:#fafbfc;font-weight:600;font-size:12px;color:var(--dim);
white-space:nowrap}
tr:last-child td{border-bottom:none}
td.nowrap{white-space:nowrap}
.chip{display:inline-block;padding:1px 7px;border-radius:99px;background:var(--chip);
font-size:12px;margin:0 4px 3px 0;white-space:nowrap}
.chip.r{background:#fdecea;color:var(--red)}
.chip.a{background:#fdf3e2;color:var(--amber)}
.chip.g{background:#e8f5ec;color:var(--green)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px}
.card.bad{border-color:#f0b4ac}
.card h3{margin:0 0 2px;font-size:15px}
.card .rows{margin:10px 0 0;font-size:13px}
.card .rows div{display:flex;justify-content:space-between;gap:10px;padding:2px 0}
.card .rows .lbl{color:var(--dim)}
.problem{background:#fdecea;border:1px solid #f0b4ac;border-radius:6px;
padding:8px 10px;margin:8px 0 0;font-size:12.5px;color:#8c2f22}
.problem li{margin:3px 0}
.problem ul{margin:4px 0 0;padding-left:18px}
button{font:inherit;padding:6px 13px;border:1px solid var(--line);background:var(--card);
border-radius:6px;cursor:pointer}
button:hover{background:#f0f2f5}
input[type=password],input[type=search]{font:inherit;padding:7px 10px;
border:1px solid var(--line);border-radius:6px;background:var(--card);width:100%}
.login{max-width:340px;margin:14vh auto;background:var(--card);
border:1px solid var(--line);border-radius:10px;padding:22px}
.login h1{font-size:17px;margin:0 0 4px}
.login p{margin:0 0 16px}
.err{color:var(--red);font-size:13px;margin:0 0 12px}
.empty{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:22px;text-align:center;color:var(--dim)}
.note{background:#fffbea;border:1px solid #f0dfa8;border-radius:6px;
padding:9px 11px;font-size:12.5px;margin:10px 0}
.tools{display:flex;gap:10px;align-items:center;margin-left:auto}
.tools input{width:200px}
.queuebar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
tr.fresh td:nth-child(2){box-shadow:inset 3px 0 0 var(--blue)}
tr.fresh td:nth-child(2)::after{content:"新";font-size:11px;color:var(--blue);
margin-left:5px;vertical-align:super}
.proj{display:flex;gap:10px;align-items:flex-start;padding:9px 0;
border-bottom:1px solid var(--line);flex-wrap:wrap}
.proj:last-child{border-bottom:none}
.proj .who{flex:1;min-width:180px}
.proj .who b{font-weight:600}
.proj .acts{display:flex;gap:6px}
.proj.off{opacity:.55}
.addbox{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:14px;margin-top:12px}
.addbox .row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}
.addbox input[type=text]{flex:1;min-width:190px;font:inherit;padding:7px 10px;
border:1px solid var(--line);border-radius:6px}
.out{margin-top:10px;font-size:13px;white-space:pre-wrap}
.ok{color:var(--green)}
code{background:var(--chip);padding:1px 5px;border-radius:4px;font-size:12px}
"""

_SCRIPT = """
(function(){
  var token = document.body.dataset.csrf || "";
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
  <h1>监控面板</h1>
  <p class=muted>小红书/抖音笔记巡检</p>
  {error}
  <form method=post action=/login>
    <input type=password name=password placeholder=口令 autofocus
           autocomplete=current-password>
    <p></p>
    <button type=submit>进去</button>
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
        return ("<div class=empty>没有需要处理的行。"
                "<br><span class=muted>风控中 / 有负面 / 置顶掉了 / 已失效 / "
                "刷新失败 / 卡住了 —— 一条都没有。</span></div>")
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
            "target=_blank rel='noopener noreferrer'>去这一行 →</a></td>"
            "</tr>")
    return (f"""<div class=queuebar>
  <button type=button id=btnQueue disabled>勾「排队刷新」（<span id=pickN>0</span>）</button>
  <span class=muted>面板不发付费请求——勾上之后由 cron 在五分钟内接手，
  和你在飞书里手工勾是同一条路。</span>
  <span id=queueOut class=muted></span>
</div>
<table id=todos><thead>{head}</thead>"""
            f"<tbody>{''.join(body)}</tbody></table>")


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
        "rel='noopener noreferrer'>去这一行 →</a></td></tr>"
        for t in todos)
    return f"""<details style='margin-top:12px'>
  <summary class=muted style='cursor:pointer'>还有 {len(todos)} 行已归档的老帖也有异常
    （不在上面的列表和批量勾选里）</summary>
  <div class=note>这些帖子已经超过归档天数、不再自动刷了。
    「排队刷新」会**绕过归档线**，所以它们刻意不混进上面那一屏——
    多数时候正确的处置是去飞书**取消巡查**，而不是再花钱刷一次。</div>
  <table><thead><tr><th>项目</th><th>为什么</th><th>诊断信息</th><th></th></tr></thead>
  <tbody>{rows}</tbody></table>
</details>"""


def _project_card(p: summary.ProjectSnapshot, offset_hours: float) -> str:
    if p.error:
        return (f"<div class='card bad'><h3>{_e(p.label)}</h3>"
                f"<div class=problem>{_e(p.error)}</div>"
                f"<div class=rows><div><a href='{_e(p.table_url)}' target=_blank "
                "rel='noopener noreferrer'>打开这张表 →</a></div></div></div>")

    health = ""
    problems = list(p.health)
    if p.estimate_blocked:
        problems.insert(0, p.estimate_blocked)
    if problems:
        items = "".join(f"<li>{_e(problem)}</li>" for problem in problems)
        health = (f"<div class=problem><b>体检发现 {len(problems)} 个问题</b>"
                  f"<ul>{items}</ul></div>")

    due_text = (f"{p.due_rows} 行 ≈ ¥{p.due_yuan:.2f}" if not p.estimate_blocked
                else "<b style='color:var(--red)'>无法估算</b>")
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
     rel='noopener noreferrer'>打开这张表 →</a></div>
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
            else f"¥{run.points_yuan:.2f}")
    started = _stamp(int(run.started_at * 1000) if run.started_at else None,
                     offset_hours)
    return ("<tr>"
            f"<td class=nowrap>{_e(started)}</td>"
            f"<td class=nowrap>{_e(run.mode or '—')}</td>"
            f"<td>{tables}</td>"
            f"<td class=nowrap>{run.rows}</td>"
            f"<td class=nowrap>¥{run.cost_yuan:.2f}</td>"
            f"<td class=nowrap>{left}</td>"
            f"<td>{badge}{''.join(extra)}</td>"
            "</tr>")


def _runs_section(runs, log_error: str, offset_hours: float) -> str:
    note = (f"<div class=note>⚠ 取 Railway 日志失败：{_e(log_error)}</div>"
            if log_error else "")
    if not runs:
        body = ("<div class=empty>暂时没有可解析的运行记录。"
                "<br><span class=muted>需要 cron 那个服务设上 "
                "<code>RUN_LOG_JSON=1</code>，日志才带结构化事件；"
                "Railway 免费/Hobby 套餐日志只留 7 天。</span></div>")
        return f"<h2>运行历史</h2>{note}{body}"
    head = ("<tr><th>开跑</th><th>模式</th><th>表</th><th>行数</th>"
            "<th>花费</th><th>跑完剩</th><th>结果</th></tr>")
    rows = "".join(_run_row(r, offset_hours) for r in runs)
    unfinished = sum(1 for r in runs if not r.finished)
    warn = ""
    if unfinished:
        warn = (f"<div class=note>有 {unfinished} 轮只有开跑没有收尾——"
                "那是被容器杀掉的形态（redeploy、回收、OOM），"
                "那几轮已经付过钱的结果多半丢了。</div>")
    return (f"<h2>运行历史（{len(runs)}）</h2>{note}{warn}"
            f"<table><thead>{head}</thead><tbody>{rows}</tbody></table>")


def _projects_section(config) -> str:
    """项目管理。**内容由前端异步拉** —— 它要读注册表，
    而注册表和聚合缓存是两条独立的路：注册表读不到不该让整个面板变空白。
    """
    patch = ("自动补选项：<b>已开</b>" if getattr(config, "allow_option_patch", False)
             else "自动补选项：<b>关</b>（缺选项只给清单，"
                  "在废表上验过再开 <code>PANEL_ALLOW_OPTION_PATCH=1</code>）")
    return f"""<h2>项目</h2>
<div class=muted style='margin-bottom:8px'>加表、停用、移除都在这儿。
「移除」只是不再监控，<b>不动你飞书表里的任何数据</b>。{patch}</div>
<div id=projects class=empty>读取中…</div>
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
        return (f"<h2>余额</h2><div class=empty>{_e(balance_error)}</div>")

    warn_days = getattr(config, "runway_warn_days", 14.0)
    alert_days = getattr(config, "runway_alert_days", 5.0)
    cards = []
    for b in balances:
        if b.error:
            cards.append(
                f"<div class='card bad'><h3>{_e(b.label)}</h3>"
                f"<div class=problem>读不到：{_e(b.error)}</div>"
                "<div class=muted>这不等于余额为 0——去对应后台看一眼，"
                "或检查 Key 是不是过期了</div></div>")
            continue
        if b.unit == "USD":
            main = f"${b.amount:.2f}"
            sub = (f"≈ ¥{b.yuan:.2f}（按 {b.rate:g} 折算）"
                   if b.yuan is not None else "")
        else:
            main = f"{b.amount:.0f} 积分"
            sub = f"≈ ¥{b.yuan:.2f}" if b.yuan is not None else ""
        credit = (f"<div class=muted>另有赠送额度 {b.free_credit:g}</div>"
                  if b.free_credit else "")
        cards.append(
            f"<div class=card><h3>{_e(b.label)}</h3>"
            f"<div style='font-size:24px;font-weight:600'>{_e(main)}</div>"
            f"<div class=muted>{_e(sub)}</div>{credit}</div>")

    note = ""
    if runway is not None and runway.known:
        cls = "problem" if runway.days <= alert_days else (
            "note" if runway.days <= warn_days else "muted")
        partial = ("；<b>只算了读得到的那家</b>，实际比这个多"
                   if runway.partial else "")
        note = (f"<div class={cls}>按最近 {runway.runs_used} 轮"
                f"（{runway.hours_covered:.1f} 小时）的实际花速 "
                f"¥{runway.yuan_per_day:.2f}/天，余额还够跑 "
                f"<b>{runway.days:.0f} 天</b>{partial}</div>")
    elif runway is not None and runway.reason:
        note = f"<div class=muted>还算不出「够跑多久」：{_e(runway.reason)}</div>"
    err = (f"<div class=note>⚠ 取余额失败：{_e(balance_error)}，"
           "下面是上一次还能用的读数。</div>" if balance_error else "")
    return (f"<h2>余额</h2>{err}{note}"
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
        inner = ("<div class=wrap><header><h1>监控面板</h1></header>"
                 "<div class=empty>正在第一次取数，稍等几秒刷新页面。"
                 + (f"<div class=problem style='margin-top:12px'>{_e(error)}</div>"
                    if error else "") + "</div></div>")
        return _shell("监控面板", inner, csrf=csrf)

    todos = overview.todos()
    # 每张表内部按 max_todos 截掉的 + 跨表拉平之后再截掉的。两个都要算，
    # 否则「要人管 200 行」在一次大面积事故里会被当成精确值。
    todos_hidden = overview.todos_dropped + overview.todos_dropped_by()
    hidden_note = (f" ⚠ 另有 {todos_hidden} 行同样需要处理，"
                   "因为条数上限没显示——先处理完这一屏再回来看。"
                   if todos_hidden else "")
    # 零个项目要说清楚是「还没加」而不是「都健康」。全零的一屏和
    # 「一切正常」长得一模一样，这是这套东西最难发现的那类故障。
    empty_note = ("" if overview.projects else
                  "<div class=note>注册表里还没有一张可用的表——"
                  "在下面「项目」那一栏加第一张。加完等一轮缓存（或点"
                  "「重新取数」）就会出现在这里。</div>")
    projects = overview.projects
    stale_note = ""
    if error:
        stale_note = (f"<div class=note>⚠ 上一次取数失败（{_e(error)}），"
                      "下面是上一份还能用的快照。</div>")
    domain_note = ""
    if config.feishu_base.rstrip("/") == "https://feishu.cn":
        domain_note = ("<div class=note>没设 <code>FEISHU_DOMAIN</code>，"
                       "「去这一行」的链接用的是通用域名，不一定跳得准。"
                       "把你们租户的域名（形如 <code>https://xxx.feishu.cn</code>）"
                       "设进去就好了。</div>")

    bar = "".join([
        _stat(overview.total_rows, "在管行数"),
        _stat(f"{len(todos)}" + ("+" if todos_hidden else ""),
              "要人管" + (f"（另有 {todos_hidden} 行未显示）" if todos_hidden else ""),
              alert=bool(todos)),
        _stat(overview.due_rows, "到期待刷"),
        _stat(f"¥{overview.due_yuan:.2f}"
              + (" +?" if overview.unestimatable else ""),
              "预计花费" + (f"（{len(overview.unestimatable)} 张表算不出）"
                          if overview.unestimatable else "")),
        _stat(overview.queued_rows, "排队中"),
        _stat(overview.stale_rows, "卡住了", alert=bool(overview.stale_rows)),
        *_runway_stat(runway, config),
    ])

    inner = f"""<div class=wrap>
<header>
  <h1>监控面板</h1>
  <span class=muted>{len(projects)} 个项目 · 数据于
    {_e(_stamp(int(fetched_at * 1000) if fetched_at else None, offset_hours))} 取得</span>
  <span class=tools>
    <input id=filter type=search placeholder='筛待办…'>
    <button id=refresh type=button>重新取数</button>
    <form method=post action=/logout style='margin:0'><button type=submit>退出</button></form>
  </span>
</header>
{stale_note}{domain_note}
<div class=bar>{bar}</div>

<h2>要人管的行（{len(todos)}{'+' if todos_hidden else ''}）</h2>
<div class=muted style='margin-bottom:8px'>跨所有项目拉平，按严重度排序。点「去这一行」
直接落到飞书表里那一行——面板负责发现，具体处理在飞书里做。{hidden_note}</div>
{_todo_table(todos, offset_hours, config.show_digest)}
{_archived_section(overview.archived_todos(), offset_hours, config.show_digest)}

<h2>各项目</h2>
{empty_note}<div class=grid>{''.join(_project_card(p, offset_hours) for p in projects)}</div>

{_projects_section(config)}

{_balance_section(balances or [], balance_error, runway, config)}

{_runs_section(runs or [], log_error, offset_hours)}

<p class=muted style='margin-top:26px'>这个面板只读飞书表，
<b>不发任何付费请求</b>。要刷新某一行，去表里勾「排队刷新」，
后台 cron 会在 5 分钟内接手。</p>
</div>"""
    return _shell("监控面板", inner, csrf=csrf)


def _runway_stat(runway, config) -> list:
    """顶栏那一格「还够跑」。算不出就整格不显示——
    顶栏放一个「—」只会占位置，还让人以为是 0。"""
    if runway is None or not runway.known:
        return []
    alert_days = getattr(config, "runway_alert_days", 5.0)
    warn_days = getattr(config, "runway_warn_days", 14.0)
    label = "还够跑" + ("（下界）" if runway.partial else "")
    return [_stat(f"{runway.days:.0f} 天", label,
                  alert=runway.days <= max(alert_days, warn_days))]


def _stat(value, key: str, *, alert: bool = False) -> str:
    cls = "stat alert" if alert else "stat"
    return (f"<div class='{cls}'>"
            f"<div class=n>{_e(value)}</div><div class=k>{_e(key)}</div></div>")
