"""The pieces every aGiTrack web page is built from.

There are three pages (the dashboard, the learn page, the storyline) and they are deliberately
three self-contained documents: each is served on its own, works from a file:// snapshot, and
carries its own script. What they must NOT be is three separate designs. Every time a shared
detail lived in three places it had to be fixed in three places, and usually was not: the
same commit message rendered in different colours, one page's ``hidden`` boxes stayed on
screen because only two pages had the rule, a diff wrapped on one page and ran off the edge
on another, the same backtrace banner had three looks.

So the shared parts live HERE, once:

* ``TOKENS`` - the colour and font variables, with the same names on every page.
* ``BASE_CSS`` - reset, page background, links, ``[hidden]``, buttons, chips, panels.
* ``BANNER_CSS`` - the frozen top strip (backtrace notice, static-demo notice, update notice).
* ``OVERLAY_CSS`` - the full-screen "the agent is working" card.
* ``COMMIT_CSS`` / ``COMMIT_JS`` - rendering a commit: its message as markdown, its diff, and
  the paragraph reflow that makes a hard-wrapped message read as prose in a narrow column.
* ``DOM_JS`` - the handful of helpers every page's script opens with.

A page takes what it needs by substituting the placeholders (``__UI_TOKENS__`` and friends)
into its template; everything specific to that page stays in that page.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- tokens

# One palette, one set of names. The dashboard used to call the same colour --panel-2 while
# the other two called it --panel2, and --red vs --bad, which is how a rule copied between
# pages silently rendered in the wrong colour (or none).
TOKENS = """:root{
  --ink:#070b09; --panel:#0c120e; --panel2:#101813; --panel-2:#101813; --line:#1d2a21;
  --phosphor:#3dffa0; --phosphor-dim:#1f7a52; --amber:#ffb454; --amber-dim:#8a5e2a;
  --fg:#cfe7d8; --fg-dim:#7e998a; --red:#ff6b6b; --bad:#ff6b6b; --ops:#67b8d6; --accent:#67b8d6;
  --warn:#ffb454; --chipbg:#101813;
  --mono:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace; --display:"VT323",var(--mono);
}"""

# --------------------------------------------------------------------------- base

BASE_CSS = """*{box-sizing:border-box}
/* No page may scroll sideways: a single over-wide element otherwise drags the whole
   document with it, which is worse on a phone than the element being clipped. */
html,body{overflow-x:hidden}
/* `hidden` is a UA-level display:none, which ANY author `display` outranks: a hidden flex or
   grid box stays on screen without this. It is the reason the dashboard's file filter showed
   in the commits view for a while. Every page gets it. */
[hidden]{display:none !important}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
/* On a phone the page IS the column: side padding that reads as breathing room on a desktop
   is a tenth of the screen on a 390px one, and every table, diff and moment card pays for it.
   Written as `body .wrap` so it outranks each page's own `.wrap` padding whatever order the
   two blocks end up in. */
@media (max-width:760px){body .wrap{padding-left:6px;padding-right:6px}}
/* The spinner every page uses while something is loading. */
.spin{width:13px;height:13px;border:2px solid var(--phosphor-dim);border-top-color:var(--phosphor);
  border-radius:50%;animation:spin .7s linear infinite;display:inline-block;flex:none}
@keyframes spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion: reduce){.spin{animation:none}}"""

# --------------------------------------------------------------------------- banners

# The frozen top strip: the backtrace notice, the static-demo notice, the update notice. The
# dashboard calls its element .backtracebanner and the other pages .btbanner (each page's own
# markup), but they look the same because the rules are the same.
BANNER_CSS = """.backtracebanner,.btbanner,.updatebanner{position:sticky;top:0;z-index:60;margin:0;padding:10px 18px;
  background:var(--panel);border-bottom:2px solid var(--amber-dim);color:var(--warn);
  font-size:12.5px;line-height:1.5;text-align:center;box-shadow:0 4px 16px rgba(0,0,0,.55)}
.backtracebanner code,.btbanner code,.updatebanner code{color:var(--fg);background:var(--panel2);
  padding:0 5px;border-radius:3px}
/* The call to action is GREEN against the amber notice, so the one thing to click stands
   apart from the explanation. */
.backtracebanner a,.btbanner a,.updatebanner a{color:var(--phosphor);text-decoration:none;
  border-bottom:1px solid var(--phosphor-dim)}
.backtracebanner a:hover,.btbanner a:hover,.updatebanner a:hover{color:var(--ink);
  background:var(--phosphor);text-decoration:none}"""

# --------------------------------------------------------------------------- a date range

# "Which days am I looking at": a preset dropdown with a custom from/to popup anchored under
# it. The dashboard filters what it SHOWS with this; the story page scopes what it TELLS.
# Same control, same look, same keyboard and date-picker behaviour, one definition.
RANGE_CSS = """input[type=date]{background:var(--ink);color:var(--fg);border:1px solid var(--line);
  font-family:var(--mono);font-size:13px;padding:6px 9px;cursor:pointer}
input[type=date]:focus{outline:none;border-color:var(--phosphor)}
input[type=date]::-webkit-calendar-picker-indicator{filter:invert(.7) sepia(1) hue-rotate(90deg)}
/* custom date range: a popup anchored under the period select */
.period-field{position:relative}
.daterange{position:absolute;top:100%;right:0;z-index:30;margin-top:8px;background:var(--panel);
  border:1px solid var(--phosphor-dim);padding:12px 14px;display:flex;gap:12px;align-items:flex-end;
  box-shadow:0 10px 28px rgba(0,0,0,.6)}
.dr-field{display:flex;flex-direction:column;gap:4px}
.dr-field label{font-size:11px;color:var(--amber);letter-spacing:.6px;text-transform:uppercase}
.dr-done{cursor:pointer;border:1px solid var(--phosphor);color:var(--phosphor);background:transparent;
  font-family:var(--mono);font-size:12.5px;padding:6px 12px}
.dr-done:hover{background:var(--phosphor);color:var(--ink)}"""

# The presets, in one place, so both pages offer the same choices in the same order.
RANGE_OPTIONS = """<option value="">all time</option>
      <option value="1">last 24 hours</option>
      <option value="7">last 7 days</option>
      <option value="30">last 30 days</option>
      <option value="90">last 90 days</option>
      <option value="custom">custom range…</option>"""

# Turning the control into two timestamps, and reflecting the choice back into the inputs.
RANGE_JS = """const DAY_SECONDS = 86400;
// Read as UTC, like every other date these pages show, so the range a reader picks means the
// same days as the stamps on the commits. "to" is the END of its day, or a commit made that
// afternoon falls outside a range that names it.
function dateToTs(value, endOfDay){
  if(!value) return 0;
  const ts = Date.parse(value + "T00:00:00Z") / 1000;
  return isNaN(ts) ? 0 : (endOfDay ? ts + DAY_SECONDS - 1 : ts);
}
const ymd = ts => ts ? new Date(ts * 1000).toISOString().slice(0, 10) : "";

// Wire the control: the preset list, the custom popup, and closing it. `apply` is the page's
// own "the range changed" handler.
//
// The click listener on the select is not redundant with change. Picking "custom range…"
// when it is ALREADY the choice fires no change event (the value did not change), so the
// popup never reopened and there was no way to go from one custom range to another without
// first selecting some other preset.
function bindRangeControl(apply){
  const period = $("f-period"), box = $("daterange");
  const open = on => { box.hidden = !on; };
  period.addEventListener("change", () => { open(period.value === "custom"); apply(); });
  period.addEventListener("click", () => { if (period.value === "custom") open(true); });
  const picked = () => { period.value = "custom"; apply(); };
  $("f-from").addEventListener("change", picked);
  $("f-to").addEventListener("change", picked);
  $("dr-done").addEventListener("click", () => open(false));
  document.addEventListener("click", event => {
    if (!box.hidden && !event.target.closest(".period-field")) open(false);
  });
}"""

# --------------------------------------------------------------------------- notices

# What a page says when something is unavailable, refused or went wrong. It floats as ONE
# fixed toast at the bottom, because a note rendered where the markup happens to sit is
# missed whenever the reader has scrolled (and, on the story page, appeared as a red block
# in the middle of the page while the same message on the learn page was a quiet toast).
# Click dismisses. Real errors stay red; everything else is amber.
FLASH_CSS = """.notice{border:1px solid var(--warn);color:var(--warn);padding:10px 14px;font-size:13px;
  margin:10px 0;border-radius:6px}
.error{border:1px solid var(--bad);color:var(--bad);padding:10px 14px;font-size:13px;
  margin:10px 0;border-radius:6px}
#flash{position:fixed;left:50%;transform:translateX(-50%);bottom:18px;z-index:80;
  width:min(680px,calc(100vw - 32px));pointer-events:none}
#flash .notice,#flash .error{pointer-events:auto;cursor:pointer;background:var(--panel);
  box-shadow:0 14px 44px rgba(0,0,0,.65);margin:6px 0}
#flash .notice::after,#flash .error::after{content:" · click to dismiss";opacity:.6;font-size:11px}"""

# --------------------------------------------------------------------------- settings

# The collapsed "who does the work" panel: the learn page's coach engine and the story
# page's storyteller are the same setting (`learning_backend` / `learning_model`), so they
# are the same panel, down to the gear on the summary.
ENGINE_CSS = """.engine{margin-top:26px;border:1px solid var(--line);background:var(--panel);border-radius:8px}
.engine summary{cursor:pointer;padding:10px 16px;color:var(--fg-dim);font-size:12.5px;list-style:none}
.engine summary::before{content:"\\2699\\FE0F  "}
.engine summary:hover{color:var(--fg)}
.engine .ebody{padding:4px 16px 14px}
.engine .esaved{color:var(--phosphor)}"""

# --------------------------------------------------------------------------- overlay

# Shown while the agent is generating: the page underneath is not usable, and pretending
# otherwise is what made people click into a half-written story.
OVERLAY_CSS = """.overlay{position:fixed;inset:0;z-index:80;display:flex;align-items:center;justify-content:center;
  background:rgba(3,6,4,.74);backdrop-filter:blur(3px);animation:fadein .25s ease both}
@keyframes fadein{from{opacity:0}to{opacity:1}}
.ov-card{background:var(--panel);border:1px solid var(--phosphor-dim);border-radius:12px;
  padding:30px 38px;max-width:460px;margin:0 20px;text-align:center;
  box-shadow:0 18px 60px rgba(0,0,0,.55)}
.ov-title{font-size:16px;color:var(--phosphor);margin:12px 0 4px}
.ov-msg{font-size:13px;color:var(--fg-dim);min-height:20px}
/* Which model is spending the reader's time and tokens, said plainly. */
.ov-engine{font-size:12px;color:var(--accent);margin-top:2px;min-height:16px}
.ov-bar{height:4px;border-radius:2px;background:var(--panel2);overflow:hidden;margin:16px 0 12px}
.ov-bar span{display:block;height:100%;width:0;border-radius:2px;background:var(--phosphor);
  transition:width .5s ease}
.ov-hint{font-size:11.5px;color:var(--fg-dim);margin-bottom:14px}"""

# --------------------------------------------------------------------------- a commit

# How a commit looks when it is opened: its message, and its file changes. Identical on the
# dashboard and in a story moment, because it is the same commit.
COMMIT_CSS = """.dmsg{font-size:12.5px;line-height:1.55;color:var(--fg-dim);word-break:break-word}
.dmsg .diffempty{color:var(--fg-dim);font-size:12px;font-style:italic;padding:8px 12px;line-height:1.55}
.dmsg .diffempty b{color:var(--phosphor);font-style:normal}
.dmsg.md p{margin:7px 0}
/* Reflow mode (set by reflowParagraphs on paragraphs the layout wraps anyway): the source
   line breaks stop rendering and the paragraph flows as prose. */
.dmsg.md p.mdp.reflow br{display:none}
.dmsg.md .md-h{font-family:var(--mono);color:var(--amber);margin:11px 0 5px;font-size:13px;font-weight:600}
/* Heading depth reads at a glance: structural sections (# …) brightest/largest, the
   ## User/## Agent role one step down, a message's own headings smaller and indented. */
.dmsg.md h3.md-h{font-size:15px;color:var(--amber)}
.dmsg.md h4.md-h{font-size:13.5px;color:var(--phosphor)}
.dmsg.md h5.md-h{font-size:12.5px;color:var(--ops);font-weight:500;padding-left:10px;border-left:2px solid var(--line)}
.dmsg.md h6.md-h{font-size:12px;color:var(--fg-dim);font-weight:500;padding-left:20px;border-left:2px solid var(--line)}
.dmsg.md ul{margin:6px 0 6px 18px} .dmsg.md li{margin:2px 0}
.dmsg.md code{background:var(--panel2);border:1px solid var(--line);padding:0 4px;color:var(--phosphor);font-size:12px}
.dmsg.md strong{color:var(--fg)} .dmsg.md em{color:var(--fg)}
.dmsg.md .md-code{white-space:pre-wrap;background:var(--panel2);border:1px solid var(--line);
  padding:8px 10px;margin:7px 0;color:var(--fg-dim);font-size:12px}
.dmsg.md a{color:var(--phosphor)}
/* The button that flips a commit between its message and its file changes. It appears in
   three places under three names (the dashboard's log detail, its file browser, and a story
   moment's commits) and must look the same in all of them: the log's one carried NO rule at
   all and rendered as the browser's default grey chrome on a black terminal page. */
.diffbtn,.fdifftoggle,.cflip{font:inherit;font-size:11.5px;color:var(--phosphor);
  background:transparent;border:1px solid var(--phosphor-dim);border-radius:4px;
  padding:1px 8px;cursor:pointer;letter-spacing:.3px;transition:background .15s,color .15s}
.diffbtn:hover,.fdifftoggle:hover,.cflip:hover{background:var(--phosphor);color:var(--ink)}
/* No frame and no scroller: the diff flows with the page exactly like the message it toggles
   with, and a long line WRAPS rather than widening the column it sits in. */
.diffbox{margin:0;font-size:12px;line-height:1.55;white-space:pre-wrap;overflow-wrap:anywhere;
  word-break:break-word}
.diffbox .dl{display:block}
.diffbox .dfile{color:var(--amber);background:rgba(255,180,84,.06)}
.diffbox .dhunk{color:var(--ops);background:rgba(103,184,214,.09)}
.diffbox .dmeta2{color:var(--fg-dim)}
.diffbox .dadd{color:var(--phosphor);background:rgba(61,255,160,.08)}
.diffbox .ddel{color:var(--red);background:rgba(255,107,107,.08)}"""

DOM_JS = """const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt = n => (n||0).toLocaleString("en-US");
const kfmt = n => { n = n||0; return n >= 1000 ? (n/1000).toFixed(n >= 10000 ? 0 : 1)+"k" : ""+n; };
async function postJson(path, body){
  const r = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"},
                               body: JSON.stringify(body || {}), cache:"no-store"});
  if(!r.ok) throw new Error("server error " + r.status);
  return r.json();
}"""

# The commit renderer. `md` here is the COMMIT message renderer (aGiTrack's own format: role
# headings, a metadata block, hard-wrapped prose); a page that also renders agent-written
# prose keeps its own small markdown function for that.
COMMIT_JS = """function commitMd(src){
  const lines = (src||"").replace(/\\r\\n/g,"\\n").split("\\n");
  const inline = t => esc(t)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\\*\\*([^*]+)\\*\\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\\*([^*\\n]+)\\*/g, "$1<em>$2</em>")
    .replace(/\\[([^\\]]+)\\]\\((https?:[^)\\s]+)\\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  let html="", inCode=false, code=[], inList=false, inMeta=false, para=[];
  const closeList = () => { if(inList){ html+="</ul>"; inList=false; } };
  // Consecutive non-blank lines are ONE paragraph, their source breaks kept as <br>. The " "
  // before each <br> is what remains when reflowParagraphs() hides the break on a narrow
  // screen, so the lines join as prose. Metadata lines are marked "keep": their breaks are
  // structure, never re-flowed.
  const flushPara = () => {
    if(!para.length) return;
    html += `<p class="mdp${inMeta?" keep":""}">` + para.map(inline).join(" <br>") + "</p>";
    para = [];
  };
  for(const raw of lines){
    if(raw.trimStart().startsWith("```")){
      flushPara();
      if(inCode){ html+="<pre class=\\"md-code\\">"+esc(code.join("\\n"))+"</pre>"; code=[]; inCode=false; }
      else { closeList(); inCode=true; }
      continue;
    }
    if(inCode){ code.push(raw); continue; }
    const h = raw.match(/^(#{1,6})\\s+(.*)$/);
    if(h){
      flushPara(); closeList();
      if(/agitrack\\s+metadata/i.test(h[2])) inMeta = true;
      const lvl=Math.min(6,h[1].length+2); html+=`<h${lvl} class="md-h">${inline(h[2])}</h${lvl}>`; continue;
    }
    const li = raw.match(/^\\s*[-*+]\\s+(.*)$/);
    if(li){ flushPara(); if(!inList){ html+="<ul>"; inList=true; } html+="<li>"+inline(li[1])+"</li>"; continue; }
    if(raw.trim()===""){ flushPara(); closeList(); continue; }
    para.push(raw);
  }
  flushPara();
  if(inCode){ html+="<pre class=\\"md-code\\">"+esc(code.join("\\n"))+"</pre>"; }
  closeList();
  return html;
}

// Commit messages are hard-wrapped by the writer (~72 chars). When the rendering column is
// narrower than that, every source line wraps AGAIN and the text reads as ragged fragments.
// So a paragraph the layout wraps further drops its single line breaks and flows as prose;
// one that fits keeps every break exactly as written.
function reflowParagraphs(root){
  if(!root || !root.querySelectorAll) return;
  for(const p of root.querySelectorAll("p.mdp:not(.keep)")){
    p.classList.remove("reflow");
    const breaks = p.getElementsByTagName("br").length;
    if(!breaks) continue;
    const lh = parseFloat(getComputedStyle(p).lineHeight) || 16;
    const range = document.createRange();
    range.selectNodeContents(p);
    const tops = [];
    for(const r of range.getClientRects()){
      if(!r.height) continue;
      if(!tops.some(t => Math.abs(t - r.top) < lh * 0.6)) tops.push(r.top);
    }
    if(tops.length > breaks + 1) p.classList.add("reflow");
  }
}

// Colour a unified diff (diffstat + patch) line by line.
function renderDiff(text){
  const rows = (text||"").replace(/\\r\\n/g,"\\n").replace(/\\n+$/,"").split("\\n").map(raw => {
    let cls = "dl";
    if(/^(diff --git |index |new file|deleted file|similarity |rename |old mode|new mode)/.test(raw)) cls="dl dfile";
    else if(raw.startsWith("@@")) cls="dl dhunk";
    else if(raw.startsWith("+++")||raw.startsWith("---")) cls="dl dmeta2";
    else if(raw.startsWith("+")) cls="dl dadd";
    else if(raw.startsWith("-")) cls="dl ddel";
    return '<span class="'+cls+'">'+(esc(raw)||"&nbsp;")+"</span>";
  });
  return '<pre class="diffbox">'+rows.join("")+"</pre>";
}"""


def render(template: str, **extra: str) -> str:
    """Substitute the shared blocks (and any page-specific ``extra``) into ``template``.

    Shared placeholders are filled FIRST and the page's own last, so a page can never be
    surprised by a token appearing inside content it substituted."""
    filled = (
        template.replace("__UI_TOKENS__", TOKENS)
        .replace("__UI_BASE_CSS__", BASE_CSS)
        .replace("__UI_BANNER_CSS__", BANNER_CSS)
        .replace("__UI_FLASH_CSS__", FLASH_CSS)
        .replace("__UI_ENGINE_CSS__", ENGINE_CSS)
        .replace("__UI_RANGE_CSS__", RANGE_CSS)
        .replace("__UI_RANGE_OPTIONS__", RANGE_OPTIONS)
        .replace("__UI_RANGE_JS__", RANGE_JS)
        .replace("__UI_OVERLAY_CSS__", OVERLAY_CSS)
        .replace("__UI_COMMIT_CSS__", COMMIT_CSS)
        .replace("__UI_DOM_JS__", DOM_JS)
        .replace("__UI_COMMIT_JS__", COMMIT_JS)
    )
    for name, value in extra.items():
        filled = filled.replace(name, value)
    return filled
