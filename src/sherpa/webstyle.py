"""Stylesheet, icons and favicon for the web UI. No inline styles anywhere (the CSP forbids them)."""
import hashlib

FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="8" fill="#1d4ed8"/>'
    '<path d="M5 24 13 10l4.5 7.5L20 14l7 10Z" fill="#fff"/>'
    '<circle cx="24.5" cy="8" r="2.8" fill="#f59e0b"/></svg>'
)

LOGO = (
    '<svg class="logo" viewBox="0 0 32 32" aria-hidden="true">'
    '<rect width="32" height="32" rx="8" class="logo-bg"/>'
    '<path d="M5 24 13 10l4.5 7.5L20 14l7 10Z" class="logo-fg"/>'
    '<circle cx="24.5" cy="8" r="2.8" class="logo-dot"/></svg>'
)

_ICONS = {
    "ok": '<circle cx="12" cy="12" r="9"/><path d="m8 12.5 2.8 2.8L16 9.5"/>',
    "alert": '<path d="M12 3.5 2.8 19.5h18.4L12 3.5Z"/><path d="M12 10v4.5M12 17.2v.3"/>',
    "down": '<circle cx="12" cy="12" r="9"/><path d="m9 9 6 6m0-6-6 6"/>',
    "unknown": '<circle cx="12" cy="12" r="9"/><path d="M9.7 9.5a2.4 2.4 0 1 1 3.4 2.2c-.7.4-1.1.9-1.1 1.6M12 16.8v.2"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "bolt": '<path d="M13 3 5 13.5h6L10 21l8-10.5h-6L13 3Z"/>',
    "lock": '<rect x="5" y="10.5" width="14" height="9.5" rx="2"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/>',
}


def icon(name: str) -> str:
    return f'<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">{_ICONS[name]}</svg>'


CSS = """
:root{
  color-scheme:light dark;
  --bg:#f3f5f8;--surface:#fff;--surface-2:#f7f9fb;--line:#e2e6eb;--line-2:#cfd5dc;
  --text:#16202b;--muted:#566474;--faint:#8693a1;
  --primary:#1d4ed8;--primary-h:#1a43b8;--on-primary:#fff;--ring:rgba(29,78,216,.35);
  --ok:#0f7a3d;--ok-bg:#e5f5ec;--ok-line:#b5e0c6;
  --warn:#8f5200;--warn-bg:#fff2d9;--warn-line:#f0d49b;
  --bad:#b3261e;--bad-bg:#fdeceb;--bad-line:#f2b9b4;
  --info:#0b5cad;--info-bg:#e7f1fb;--info-line:#b7d4f0;
  --shadow:0 1px 2px rgba(16,24,40,.05),0 1px 3px rgba(16,24,40,.06);
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0d1117;--surface:#151b23;--surface-2:#1a212b;--line:#262e39;--line-2:#36404d;
  --text:#e7ebf0;--muted:#9aa6b4;--faint:#6f7c8b;
  --primary:#5b8def;--primary-h:#7aa3f5;--on-primary:#0b1220;--ring:rgba(91,141,239,.45);
  --ok:#4cc27a;--ok-bg:#10281b;--ok-line:#1d4a30;
  --warn:#e6ac3c;--warn-bg:#2d2310;--warn-line:#5a4519;
  --bad:#ff7b70;--bad-bg:#341512;--bad-line:#6b2a25;
  --info:#6db1ff;--info-bg:#0f2640;--info-line:#1f4a78;
  --shadow:0 1px 2px rgba(0,0,0,.4);
}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;font-variant-numeric:tabular-nums}
a{color:var(--primary);text-decoration:none}a:hover{text-decoration:underline}
:focus-visible{outline:2px solid var(--primary);outline-offset:2px;border-radius:4px}
h1,h2,h3{margin:0;line-height:1.25}
code,.mono{font-family:var(--mono);font-size:.9em}
code{background:var(--surface-2);border:1px solid var(--line);padding:1px 6px;border-radius:5px}
.mute{color:var(--muted)}.faint{color:var(--faint)}.small{font-size:13px}.right{text-align:right}.nowrap{white-space:nowrap}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}

/* ---------- top bar ---------- */
.topbar{position:sticky;top:0;z-index:10;background:var(--surface);border-bottom:1px solid var(--line);box-shadow:var(--shadow)}
.topbar-in{max-width:1180px;margin:0 auto;padding:0 20px;display:flex;align-items:center;gap:8px 22px;min-height:58px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:10px;color:var(--text);font-weight:700;letter-spacing:.02em}
.brand:hover{text-decoration:none}
.brand small{display:block;font-weight:500;font-size:11px;letter-spacing:.04em;color:var(--muted);line-height:1.1}
.logo{width:30px;height:30px;flex:none}.logo-bg{fill:var(--primary)}.logo-fg{fill:var(--on-primary)}.logo-dot{fill:#f59e0b}
.nav{display:flex;gap:2px;flex:1;overflow-x:auto}
.nav a{padding:17px 12px 15px;color:var(--muted);font-weight:500;border-bottom:3px solid transparent;white-space:nowrap}
.nav a:hover{color:var(--text);text-decoration:none}
.nav a[aria-current=page]{color:var(--text);border-bottom-color:var(--primary)}
.top-right{display:flex;align-items:center;gap:12px}
.pill{display:inline-flex;align-items:center;gap:7px;padding:4px 11px;border-radius:999px;font-size:13px;font-weight:600;border:1px solid}
.pill i{width:8px;height:8px;border-radius:50%;background:currentColor}
.pill.ok{color:var(--ok);background:var(--ok-bg);border-color:var(--ok-line)}
.pill.bad{color:var(--bad);background:var(--bad-bg);border-color:var(--bad-line)}
.pill.warn{color:var(--warn);background:var(--warn-bg);border-color:var(--warn-line)}
.pill.unknown{color:var(--muted);background:var(--surface-2);border-color:var(--line)}
.inline{display:inline;margin:0}

/* ---------- layout ---------- */
.wrap{max-width:1180px;margin:0 auto;padding:24px 20px 56px}
.page-head{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:8px 16px;margin-bottom:18px}
.page-head h1{font-size:22px}.page-head p{margin:2px 0 0;color:var(--muted)}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);margin-bottom:18px}
.card-h{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:4px 12px;padding:14px 18px;border-bottom:1px solid var(--line)}
.card-h h2{font-size:15px;font-weight:650}.card-h .meta{font-size:13px;color:var(--muted)}
.card-b{padding:16px 18px}.card-b.flush{padding:0}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:18px;align-items:start}.cols>.card{margin-bottom:0}
@media (max-width:860px){.cols{grid-template-columns:1fr}.cols>.card{margin-bottom:18px}}
footer{max-width:1180px;margin:0 auto;padding:0 20px 28px;color:var(--faint);font-size:12px;display:flex;flex-wrap:wrap;gap:4px 18px}

/* ---------- hero ---------- */
.hero{display:flex;gap:16px;align-items:flex-start;padding:18px 20px;border-radius:12px;border:1px solid;margin-bottom:18px;box-shadow:var(--shadow)}
.hero .ico{width:34px;height:34px;flex:none;margin-top:2px}
.hero h1{font-size:20px}.hero p{margin:3px 0 0;color:var(--text);opacity:.85}
.hero.ok{background:var(--ok-bg);border-color:var(--ok-line);color:var(--ok)}
.hero.bad{background:var(--bad-bg);border-color:var(--bad-line);color:var(--bad)}
.hero.unknown{background:var(--surface-2);border-color:var(--line-2);color:var(--muted)}
.hero .body{flex:1;min-width:0}
.alert-list{margin:12px 0 0;padding:0;list-style:none;display:grid;gap:8px}
.alert-list li{background:var(--surface);color:var(--text);border:1px solid var(--bad-line);border-radius:8px;padding:9px 12px;font-size:14px}
.alert-list b{display:block;color:var(--bad)}.alert-list span{color:var(--muted);font-size:13px}

/* ---------- KPI tiles ---------- */
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:18px}
@media (max-width:980px){.kpis{grid-template-columns:repeat(2,1fr)}}
.kpi{background:var(--surface);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);padding:14px 16px}
.kpi .k{font-size:12px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;color:var(--muted)}
.kpi .v{font-size:26px;font-weight:650;line-height:1.2;margin-top:4px}
.kpi .s{font-size:13px;color:var(--muted);margin-top:2px}
.kpi .v.bad{color:var(--bad)}.kpi .v.ok{color:var(--ok)}

/* ---------- devices ---------- */
.devs{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px;padding:16px 18px}
.dev{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:var(--surface-2)}
.dev-top{display:flex;justify-content:space-between;align-items:center;gap:8px}
.dev h3{font-size:15px}.dev .sub{font-size:13px;color:var(--muted);margin-top:2px}
.dev .state{margin-top:10px;font-size:14px;display:flex;gap:8px;align-items:baseline}
.dev .state i{width:9px;height:9px;border-radius:50%;flex:none;transform:translateY(0)}
.state.ok{color:var(--ok)}.state.bad{color:var(--bad)}.state.unknown{color:var(--muted)}
.state i{background:currentColor}

/* ---------- badges ---------- */
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;border:1px solid;white-space:nowrap}
.badge.ok{color:var(--ok);background:var(--ok-bg);border-color:var(--ok-line)}
.badge.warn{color:var(--warn);background:var(--warn-bg);border-color:var(--warn-line)}
.badge.bad{color:var(--bad);background:var(--bad-bg);border-color:var(--bad-line)}
.badge.info{color:var(--info);background:var(--info-bg);border-color:var(--info-line)}
.badge.mute{color:var(--muted);background:var(--surface-2);border-color:var(--line)}

/* ---------- tables ---------- */
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:14px}
th{position:sticky;top:0;background:var(--surface-2);text-align:left;font-size:12px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;color:var(--muted);padding:9px 14px;border-bottom:1px solid var(--line)}
td{padding:10px 14px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
tbody tr:hover td{background:var(--surface-2)}
td.t{white-space:nowrap}td.t small{display:block;color:var(--muted);font-size:12px}
td.det{max-width:360px;color:var(--muted);overflow-wrap:anywhere}
.empty{padding:34px 18px;text-align:center;color:var(--muted)}
.empty b{display:block;color:var(--text);margin-bottom:2px}
@media (max-width:700px){.hide-sm{display:none}td,th{padding:9px 10px}}

/* ---------- filter chips ---------- */
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chips{margin-bottom:16px}
.chip{padding:5px 13px;border-radius:999px;border:1px solid var(--line-2);color:var(--muted);font-size:13px;font-weight:500;background:var(--surface)}
.chip:hover{text-decoration:none;color:var(--text);border-color:var(--muted)}
.chip[aria-current=true]{background:var(--primary);border-color:var(--primary);color:var(--on-primary)}

/* ---------- chart ---------- */
.chart{width:100%;height:auto;display:block}
.chart .grid{stroke:var(--line);stroke-width:1}.chart .ax{fill:var(--faint);font-size:11px}
.chart .bar{fill:var(--primary)}.chart .bar.warn{fill:var(--warn)}.chart .bar.bad{fill:var(--bad)}
.stats{display:flex;flex-wrap:wrap;gap:6px 26px;margin-top:12px;font-size:14px}
.stats span{color:var(--muted)}.stats b{color:var(--text)}

/* ---------- forms ---------- */
.fgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:16px 20px}
.field label{display:block;font-size:13px;font-weight:600;margin-bottom:5px}
.field .hint{font-size:12px;color:var(--muted);margin-top:5px}
input[type=text],input[type=password],select{font:inherit;width:100%;padding:8px 11px;border:1px solid var(--line-2);border-radius:8px;background:var(--surface);color:var(--text);min-height:38px}
input:hover,select:hover{border-color:var(--faint)}
input:focus,select:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px var(--ring)}
.ig{display:flex}.ig input{border-top-right-radius:0;border-bottom-right-radius:0;min-width:0}
.ig .u{display:flex;align-items:center;padding:0 11px;border:1px solid var(--line-2);border-left:0;border-radius:0 8px 8px 0;background:var(--surface-2);color:var(--muted);font-size:13px}
.btn{display:inline-flex;align-items:center;gap:7px;font:inherit;font-weight:600;padding:9px 18px;border-radius:8px;border:1px solid var(--primary);background:var(--primary);color:var(--on-primary);cursor:pointer;min-height:38px}
.btn:hover{background:var(--primary-h);border-color:var(--primary-h);text-decoration:none}
.btn.ghost{background:transparent;color:var(--text);border-color:var(--line-2)}
.btn.ghost:hover{background:var(--surface-2);border-color:var(--faint)}
.btn.sm{padding:5px 12px;min-height:32px;font-size:13px}
.btn .ico{width:17px;height:17px}
.ico{width:1.2em;height:1.2em;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;vertical-align:-.2em}

details.devcard{border:1px solid var(--line);border-radius:10px;background:var(--surface);margin:0 18px 12px}
details.devcard:first-of-type{margin-top:16px}
details.devcard summary{cursor:pointer;list-style:none;padding:12px 14px;display:flex;flex-wrap:wrap;align-items:center;gap:6px 12px;font-weight:600}
details.devcard summary::-webkit-details-marker{display:none}
details.devcard summary::before{content:"";width:8px;height:8px;border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);transform:rotate(-45deg);transition:transform .15s;flex:none}
details.devcard[open] summary::before{transform:rotate(45deg)}
details.devcard summary .mute{font-weight:400}
details.devcard .dc-b{padding:6px 14px 16px;border-top:1px solid var(--line)}
details.devcard .dc-b .fgrid{margin-top:12px}
details.devcard.add summary{color:var(--primary)}
.remove{display:flex;align-items:center;gap:8px;margin-top:14px;color:var(--bad);font-size:14px;font-weight:500}
.remove input{width:16px;height:16px;accent-color:var(--bad)}
.savebar{position:sticky;bottom:0;z-index:5;background:var(--surface);border-top:1px solid var(--line);margin:0 -20px;padding:12px 20px;display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center;box-shadow:0 -4px 12px rgba(16,24,40,.06)}
.savebar .mute{font-size:13px}
.notice{display:flex;gap:10px;align-items:flex-start;padding:12px 16px;border-radius:10px;border:1px solid;margin-bottom:18px}
.notice.ok{background:var(--ok-bg);border-color:var(--ok-line);color:var(--ok)}
.notice.bad{background:var(--bad-bg);border-color:var(--bad-line);color:var(--bad)}
.notice .txt{color:var(--text)}.notice ul{margin:6px 0 0 18px;padding:0}
.notice b{color:inherit}

/* ---------- login ---------- */
.login{max-width:400px;margin:56px auto 0}
.login .card-b{padding:26px}
.login .logo{width:44px;height:44px;margin-bottom:14px}
.login h1{font-size:20px;margin-bottom:4px}.login p{margin:0 0 18px;color:var(--muted)}
.login .btn{width:100%;justify-content:center;margin-top:16px}
@media (max-width:640px){
  body{font-size:14.5px}
  .topbar-in{padding:8px 14px 0;gap:6px 12px}
  .brand small{display:none}
  .top-right{margin-left:auto}
  .nav{order:3;flex:1 0 100%;margin:0 -14px;padding:0 6px;border-top:1px solid var(--line);overflow:visible}
  .nav a{flex:1;text-align:center;padding:10px 6px 8px}
  .wrap{padding:16px 14px 40px}
  .page-head h1{font-size:20px}
  .hero{padding:14px;gap:12px}.hero .ico{width:26px;height:26px}.hero h1{font-size:18px}
  .alert-list li{padding:8px 10px}
  .kpis{gap:10px}.kpi{padding:12px}.kpi .v{font-size:22px}
  .card-h,.card-b{padding-left:14px;padding-right:14px}.devs{padding:12px 14px}
  details.devcard{margin:0 14px 12px}
  .savebar{margin:0 -14px;padding:10px 14px}.savebar .mute{display:none}
  .login{margin-top:20px}
  .badge{white-space:normal;text-align:center}td.t{white-space:normal}
}
@media print{.topbar,.savebar,footer{display:none}body{background:#fff}}
"""

CSS_VERSION = hashlib.sha1(CSS.encode()).hexdigest()[:10]  # cache-buster: the URL changes whenever the CSS does
