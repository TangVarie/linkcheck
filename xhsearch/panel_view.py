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
    head = ("<tr><th>项目</th><th>为什么</th><th>诊断信息</th>"
            "<th>评论数</th><th>最近检查</th><th></th></tr>")
    body = []
    for todo in todos:
        extra = ""
        if show_digest and (todo.negative_digest or todo.digest):
            extra = (f"<div class=muted style='margin-top:4px'>"
                     f"{_e(todo.negative_digest or todo.digest)[:300]}</div>")
        body.append(
            "<tr>"
            f"<td class=nowrap>{_e(todo.project)}</td>"
            f"<td>{_chips(todo.reasons, _REASON_CLASS)}</td>"
            f"<td>{_e(todo.diagnosis) or '<span class=muted>—</span>'}{extra}</td>"
            f"<td class=nowrap>{_e(todo.comment_count) if todo.comment_count is not None else '—'}</td>"
            f"<td class=nowrap>{_e(_stamp(todo.checked_at_ms, offset_hours))}</td>"
            f"<td class=nowrap><a href='{_e(todo.record_url)}' "
            "target=_blank rel='noopener noreferrer'>去这一行 →</a></td>"
            "</tr>")
    return (f"<table id=todos><thead>{head}</thead>"
            f"<tbody>{''.join(body)}</tbody></table>")


def _project_card(p: summary.ProjectSnapshot, offset_hours: float) -> str:
    if p.error:
        return (f"<div class='card bad'><h3>{_e(p.label)}</h3>"
                f"<div class=problem>{_e(p.error)}</div>"
                f"<div class=rows><div><a href='{_e(p.table_url)}' target=_blank "
                "rel='noopener noreferrer'>打开这张表 →</a></div></div></div>")

    health = ""
    if p.health:
        items = "".join(f"<li>{_e(problem)}</li>" for problem in p.health)
        health = (f"<div class=problem><b>体检发现 {len(p.health)} 个问题</b>"
                  f"<ul>{items}</ul></div>")

    seed_gap = p.total_rows - p.seed_keyword_rows
    neg_gap = p.total_rows - p.negative_keyword_rows
    coverage = (
        f"<div><span class=lbl>评论关键词</span><span>已填 {p.seed_keyword_rows}"
        f"/{p.total_rows}{f'（{seed_gap} 行没填）' if seed_gap else ''}</span></div>"
        f"<div><span class=lbl>负面词</span><span>已填 {p.negative_keyword_rows}"
        f"/{p.total_rows}{f'（{neg_gap} 行没填）' if neg_gap else ''}</span></div>")

    card_class = "card" if p.healthy else "card bad"
    return f"""<div class='{card_class}'>
  <h3>{_e(p.label)}</h3>
  <div class=muted><a href='{_e(p.table_url)}' target=_blank
     rel='noopener noreferrer'>打开这张表 →</a></div>
  {health}
  <div class=rows>
    <div><span class=lbl>在管</span><span>{p.total_rows} 行
      {f'<span class=muted>（{p.archived_rows} 已归档）</span>' if p.archived_rows else ''}</span></div>
    <div><span class=lbl>要人管</span><span>{p.needs_attention} 行</span></div>
    <div><span class=lbl>到期待刷</span><span>{p.due_rows} 行 ≈ ¥{p.due_yuan:.2f}</span></div>
    <div><span class=lbl>排队中</span><span>{p.queued_rows} 行</span></div>
    <div><span class=lbl>卡住了</span><span>{p.stale_rows} 行
      {f'<span class=muted>（{p.never_checked_rows} 从没刷过）</span>' if p.never_checked_rows else ''}</span></div>
    <div><span class=lbl>最旧检查</span><span>{_e(_stamp(p.oldest_checked_ms, offset_hours))}</span></div>
    {coverage}
  </div>
  <div style='margin-top:9px'>{_counts_chips(p.traffic_tag_counts)}</div>
  <div style='margin-top:5px'>{_counts_chips(p.refresh_status_counts)}</div>
</div>"""


def overview_page(*, overview: Optional[summary.Overview], error: str,
                  fetched_at: float, config, csrf: str = "",
                  offset_hours: float = 8.0) -> str:
    if overview is None:
        inner = ("<div class=wrap><header><h1>监控面板</h1></header>"
                 "<div class=empty>正在第一次取数，稍等几秒刷新页面。"
                 + (f"<div class=problem style='margin-top:12px'>{_e(error)}</div>"
                    if error else "") + "</div></div>")
        return _shell("监控面板", inner, csrf=csrf)

    todos = overview.todos()
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
        _stat(len(todos), "要人管", alert=bool(todos)),
        _stat(overview.due_rows, "到期待刷"),
        _stat(f"¥{overview.due_yuan:.2f}", "预计花费"),
        _stat(overview.queued_rows, "排队中"),
        _stat(overview.stale_rows, "卡住了", alert=bool(overview.stale_rows)),
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

<h2>要人管的行（{len(todos)}）</h2>
<div class=muted style='margin-bottom:8px'>跨所有项目拉平。点「去这一行」直接落到
飞书表里那一行——面板负责发现，具体处理在飞书里做。</div>
{_todo_table(todos, offset_hours, config.show_digest)}

<h2>各项目</h2>
<div class=grid>{''.join(_project_card(p, offset_hours) for p in projects)}</div>

<p class=muted style='margin-top:26px'>这个面板只读飞书表，
<b>不发任何付费请求</b>。要刷新某一行，去表里勾「排队刷新」，
后台 cron 会在 5 分钟内接手。</p>
</div>"""
    return _shell("监控面板", inner, csrf=csrf)


def _stat(value, key: str, *, alert: bool = False) -> str:
    cls = "stat alert" if alert else "stat"
    return (f"<div class='{cls}'>"
            f"<div class=n>{_e(value)}</div><div class=k>{_e(key)}</div></div>")
