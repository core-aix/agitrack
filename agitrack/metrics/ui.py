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
/* The one COMMAND in a notice, as opposed to a quoted value: everything else on the strip is
   context, and this is the single thing the reader can act on, so it carries the same green the
   call-to-action link does instead of blending into the amber explanation. */
.backtracebanner code.cmd,.btbanner code.cmd,.updatebanner code.cmd{color:var(--phosphor);
  background:var(--panel2);border:1px solid var(--phosphor-dim);font-weight:600}
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
/* Every property a host page might have opinions about is set HERE, including the ones this
   block would otherwise leave to inheritance. The story page styles its own section headings
   (".commits h4") in small caps, and that rule reached into a commit message's "## User"
   heading and rendered it in UPPERCASE - the same commit, two different looks, which is the
   one thing the shared renderer exists to prevent. */
.dmsg.md .md-h{font-family:var(--mono);color:var(--amber);margin:11px 0 5px;font-size:13px;font-weight:600;
  text-transform:none;letter-spacing:normal;text-align:left;font-style:normal}
/* Heading depth reads at a glance: structural sections (# …) brightest/largest, the
   ## User/## Agent role one step down, a message's own headings smaller and indented. */
.dmsg.md h3.md-h{font-size:15px;color:var(--amber)}
.dmsg.md h4.md-h{font-size:13.5px;color:var(--phosphor)}
.dmsg.md h5.md-h{font-size:12.5px;color:var(--ops);font-weight:500;padding-left:10px;border-left:2px solid var(--line)}
.dmsg.md h6.md-h{font-size:12px;color:var(--fg-dim);font-weight:500;padding-left:20px;border-left:2px solid var(--line)}
/* Lists pin their own indent for the same reason the headings do: the story page indents its
   OWN markdown lists with a padding-left, and with only a margin set here that padding stacked
   on top, so the same commit's bullets sat further in on one page than the other. */
.dmsg.md ul,.dmsg.md ol{margin:6px 0 6px 18px;padding-left:0;list-style-position:outside}
.dmsg.md li{margin:2px 0}
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

# --------------------------------------------------------------------------- the hub bar
#
# One dashboard serves every repository, in either of two views, so every page needs the same two
# controls: WHICH REPOSITORY am I looking at, and WHICH VIEW of it. They sit in one strip above
# the page's own header, identical on the dashboard, the learn page and the storyline, because
# they answer the same question wherever you are and moving between pages must not move them.
#
# The strip is built entirely from the URL plus one cheap ``/repos`` fetch, and it REMOVES ITSELF
# when neither is available: the same page code also serves from a standalone daemon and from the
# static export (file://), where there is no hub, no sibling repositories, and no second view to
# switch to. A control that cannot do anything is worse than no control.

HUBBAR_CSS = """.hubbar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
  padding:7px 18px;background:var(--panel);border-bottom:1px solid var(--line);font-size:12.5px}
.hubbar .hb-label{color:var(--fg-dim);letter-spacing:.6px;text-transform:uppercase;font-size:10.5px}
.hubbar .hb-group{display:flex;align-items:center;gap:7px}
.hubbar .hb-state{color:var(--fg-dim);display:flex;align-items:center;gap:6px}
.hubbar .hb-dot{width:7px;height:7px;border-radius:50%;background:var(--fg-dim);flex:none}
.hubbar .hb-state.live .hb-dot{background:var(--phosphor);box-shadow:0 0 6px var(--phosphor-dim)}
.hubbar .hb-state.live{color:var(--phosphor)}
/* ---- the repository picker -------------------------------------------------------------
   A native <select> cannot be given a scrollbar, a two-line row, or a pinned footer item, and
   its popup is drawn by the OS in a light theme on every platform we do not control. This is a
   button plus a listbox, so the list scrolls at a bounded height however many repositories
   there are, and "track another repository" stays reachable at the bottom of it. */
.repo-picker{position:relative}
.repobtn{display:flex;align-items:center;gap:8px;max-width:min(52vw,420px);
  background:var(--ink);color:var(--fg);border:1px solid var(--line);cursor:pointer;
  font-family:var(--mono);font-size:12.5px;padding:4px 9px}
.repobtn:hover{border-color:var(--phosphor-dim)}
.repobtn[aria-expanded="true"]{border-color:var(--phosphor)}
.repobtn .rb-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.repobtn .rb-caret{color:var(--fg-dim);flex:none}
.repomenu{position:absolute;top:100%;left:0;z-index:70;margin-top:6px;min-width:min(92vw,420px);
  max-width:min(92vw,560px);background:var(--panel);border:1px solid var(--phosphor-dim);
  box-shadow:0 12px 32px rgba(0,0,0,.65);display:flex;flex-direction:column}
/* The SCROLLING part is the list alone, so the footer action never scrolls out of reach.
   Bounded by the viewport, not by a fixed pixel count: on a short window a fixed max-height
   would push the footer off the bottom of the screen, which is the bug this is avoiding. */
.repolist{overflow-y:auto;max-height:min(52vh,340px);overscroll-behavior:contain}
.repolist::-webkit-scrollbar{width:9px}
.repolist::-webkit-scrollbar-track{background:var(--ink)}
.repolist::-webkit-scrollbar-thumb{background:var(--line);border:2px solid var(--ink)}
.repolist::-webkit-scrollbar-thumb:hover{background:var(--phosphor-dim)}
.repolist{scrollbar-width:thin;scrollbar-color:var(--line) var(--ink)}
.repoitem{display:block;width:100%;text-align:left;background:transparent;border:0;cursor:pointer;
  font-family:var(--mono);font-size:12.5px;padding:7px 12px;color:var(--fg);
  border-bottom:1px solid var(--line)}
.repoitem:last-child{border-bottom:0}
.repoitem:hover,.repoitem.cursor{background:var(--panel2)}
/* Name on the left, tracking state on the right, path underneath. The state is the reason the
   row is two lines rather than one: "which of my projects is actually being tracked right now"
   is a question the switcher can answer for every repository at once, and a list of bare names
   answers it for none of them. */
.repoitem .ri-top{display:flex;align-items:baseline;gap:10px;justify-content:space-between}
.repoitem .ri-name{color:var(--fg);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.repoitem .ri-state{flex:none;display:flex;align-items:center;gap:5px;font-size:11px;color:var(--fg-dim)}
.repoitem .ri-state.live{color:var(--phosphor)}
.repoitem .ri-dot{width:6px;height:6px;border-radius:50%;background:var(--fg-dim);flex:none}
.repoitem .ri-state.live .ri-dot{background:var(--phosphor);box-shadow:0 0 5px var(--phosphor-dim)}
.repoitem .ri-path{display:block;color:var(--fg-dim);font-size:11px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.repoitem.on .ri-name{color:var(--phosphor);font-weight:600}
.repoitem.on .ri-name::after{content:" ✓"}
.repohelp{flex:none;width:100%;text-align:left;cursor:pointer;background:var(--panel2);
  border:0;border-top:1px solid var(--phosphor-dim);color:var(--phosphor);
  font-family:var(--mono);font-size:12px;padding:8px 12px}
.repohelp:hover{background:var(--phosphor);color:var(--ink)}
/* ---- the view toggle -------------------------------------------------------------------
   Two states, both always visible: which one you are in is only legible next to the one you
   are not in, and the pair also says the other view EXISTS, which a single button labelled
   with the other mode never manages to. */
.viewtoggle{display:inline-flex;border:1px solid var(--line)}
.viewtoggle a{padding:4px 11px;color:var(--fg-dim);text-decoration:none;border-right:1px solid var(--line)}
.viewtoggle a:last-child{border-right:0}
.viewtoggle a:hover{color:var(--fg);background:var(--panel2);text-decoration:none}
.viewtoggle a.on{background:var(--phosphor);color:var(--ink);font-weight:600}
.viewtoggle a.on:hover{background:var(--phosphor);color:var(--ink)}
/* The backtrace half is amber wherever it is the CURRENT view, matching the warning strip under
   it: the reconstruction is the inferred view, and the page says so in one colour throughout. */
.viewtoggle a.on.bt{background:var(--amber);color:var(--ink)}
.viewtoggle a.on.bt:hover{background:var(--amber);color:var(--ink)}
/* ---- the "how do I add a repository?" dialog ------------------------------------------- */
.hubmodal{position:fixed;inset:0;z-index:90;display:flex;align-items:center;justify-content:center;
  background:rgba(0,0,0,.62);padding:20px}
.hubmodal .hm-box:focus{outline:none}
.hubmodal .hm-box{background:var(--panel);border:1px solid var(--phosphor-dim);max-width:620px;
  width:100%;max-height:84vh;overflow-y:auto;padding:22px 24px;box-shadow:0 18px 48px rgba(0,0,0,.7)}
.hubmodal h3{font-family:var(--display);color:var(--phosphor);font-size:22px;margin:0 0 10px}
.hubmodal p{color:var(--fg);font-size:13px;line-height:1.65;margin:0 0 12px}
.hubmodal code{background:var(--ink);color:var(--phosphor);padding:1px 6px;border:1px solid var(--line)}
.hubmodal .hm-steps{margin:0 0 14px;padding-left:18px;color:var(--fg);font-size:13px;line-height:1.9}
.hubmodal .hm-close{cursor:pointer;border:1px solid var(--phosphor);color:var(--phosphor);
  background:transparent;font-family:var(--mono);font-size:13px;padding:7px 16px}
.hubmodal .hm-close:hover{background:var(--phosphor);color:var(--ink)}
@media (max-width:760px){.hubbar{padding:7px 8px;gap:9px}.hubbar .hb-label{display:none}}"""

HUBBAR_HTML = """<div class="hubbar" id="hubbar" hidden>
  <div class="hb-group repo-picker">
    <span class="hb-label">repo</span>
    <button class="repobtn" id="hub-repo-btn" aria-haspopup="listbox" aria-expanded="false"
            title="Switch to another repository aGiTrack knows about">
      <span class="rb-name" id="hub-repo-name">this repository</span><span class="rb-caret">&#9662;</span>
    </button>
    <div class="repomenu" id="hub-repo-menu" hidden role="listbox" aria-label="Repositories">
      <div class="repolist" id="hub-repo-list"></div>
      <button class="repohelp" id="hub-repo-help">&#43; show another repository here&hellip;</button>
    </div>
  </div>
  <div class="hb-group" id="hub-views" hidden><span class="hb-label">view</span>
    <span class="viewtoggle">
      <a id="hub-active" href="#" title="aGiTrack's own tracking: commits it recorded, with the conversation and tokens behind each one">tracked</a>
      <a id="hub-backtrace" class="bt" href="#" title="Reconstructed from your local agent transcripts: inferred, not recorded">backtrace</a>
    </span></div>
  <div class="hb-group hb-state" id="hub-state" hidden><span class="hb-dot"></span><span id="hub-state-text"></span></div>
</div>
<div class="hubmodal" id="hub-help" hidden>
  <div class="hm-box" tabindex="-1">
    <h3>Showing another repository here</h3>
    <p>This dashboard lists every repository aGiTrack has worked in on this machine. A project
    appears the moment you point aGiTrack at it, and stays until you stop it.</p>
    <ol class="hm-steps">
      <li>Open a terminal in the project you want to add.</li>
      <li>Run <code>agitrack</code> and pick a mode. Every mode tracks the work and adds the
      project here.</li>
      <li>Only want to look, without tracking anything? <code>agitrack -d</code> adds it too.</li>
      <li>Never used an agent there through aGiTrack? <code>agitrack --backtrace</code> still
      reconstructs what past sessions did, and adds the project here.</li>
    </ol>
    <p>To remove one, run <code>agitrack stop</code> in it. That stops whatever aGiTrack is doing
    there and drops it from this list, without losing anything it already recorded.</p>
    <button class="hm-close" id="hub-help-close">got it</button>
  </div>
</div>"""

# ``__UI_HUBBAR_PAGE__`` is substituted per page with "", "learn" or "story", so the toggle keeps
# you on the page you are reading instead of dropping you back on the dashboard.
HUBBAR_JS = """// Where am I? The hub mounts every repository at /<r|b>/<slug>/, and the page's own sub-path
// follows. Anything else (a standalone daemon, the static export) has no hub, and the bar hides.
const HUB = (() => {
  const m = /^\\/(r|b)\\/([^/]+)\\//.exec(location.pathname);
  if(!m) return null;
  return {view: m[1] === "b" ? "backtrace" : "active", slug: m[2], page: "__UI_HUBBAR_PAGE__"};
})();
function hubUrl(view, slug){
  return "/" + (view === "backtrace" ? "b" : "r") + "/" + encodeURIComponent(slug) + "/" + HUB.page;
}
// Switching REPOSITORY goes through /go/, which redirects to whichever view suits that project.
// Carrying the current view across is what lands you on another project's empty tracked
// dashboard, which is the exact empty page the view-selection rule exists to avoid.
function hubGoUrl(slug){
  return "/go/" + encodeURIComponent(slug) + "/" + HUB.page;
}
function openHubHelp(){
  const m = $("hub-help"); if(!m) return;
  m.hidden = false;
  // Focused so Escape reaches the dialog's own handler rather than needing a page-wide one.
  const box = m.querySelector(".hm-box"); if(box) box.focus();
}
function closeHubHelp(){
  const m = $("hub-help"); if(!m) return;
  m.hidden = true;
  const btn = $("hub-repo-btn"); if(btn) btn.focus();
}

function wireRepoPicker(repos){
  const btn = $("hub-repo-btn"), menu = $("hub-repo-menu"), list = $("hub-repo-list");
  const picker = btn.parentElement;
  const here = repos.find(r => r.slug === HUB.slug);
  $("hub-repo-name").textContent = here ? here.name : "this repository";
  btn.title = here ? here.path : btn.title;
  list.innerHTML = repos.map(r =>
    `<button class="repoitem${r.slug === HUB.slug ? " on" : ""}" role="option" data-slug="${esc(r.slug)}"
       aria-selected="${r.slug === HUB.slug}" title="${esc(r.state_detail || "")}">
       <span class="ri-top"><span class="ri-name">${esc(r.name)}</span>
       <span class="ri-state${r.running ? " live" : ""}"><span class="ri-dot"></span>${esc(r.state || "")}</span></span>
       <span class="ri-path">${esc(r.path)}</span></button>`).join("");
  const items = Array.from(list.querySelectorAll(".repoitem"));
  items.forEach(el => el.onclick = () => { location.href = hubGoUrl(el.dataset.slug); });
  let cursor = Math.max(0, items.findIndex(el => el.classList.contains("on")));
  const mark = () => items.forEach((el, i) => el.classList.toggle("cursor", i === cursor));
  // The states were read when the page loaded, and a tracker can start or stop while it sits
  // open. Re-read them each time the menu is opened and update the chips in place, so the cursor
  // and the scroll position survive: rebuilding the list would move the reader's place under them.
  const refreshStates = async () => {
    let fresh = [];
    try{ fresh = (await (await fetch("/repos", {cache:"no-store"})).json()).repos || []; }catch(e){ return; }
    const by = {};
    fresh.forEach(r => by[r.slug] = r);
    items.forEach(el => {
      const r = by[el.dataset.slug]; if(!r) return;
      const chip = el.querySelector(".ri-state"); if(!chip) return;
      chip.classList.toggle("live", !!r.running);
      chip.lastChild.textContent = r.state || "";
      el.title = r.state_detail || "";
    });
  };
  const open = () => {
    menu.hidden = false; btn.setAttribute("aria-expanded", "true"); mark(); refreshStates();
    // Keep the current repository in view: with a long list the checked one is otherwise
    // somewhere below the fold of a menu that just opened scrolled to the top.
    if(items[cursor]) items[cursor].scrollIntoView({block: "nearest"});
  };
  const close = () => { menu.hidden = true; btn.setAttribute("aria-expanded", "false"); };
  btn.onclick = e => { e.stopPropagation(); menu.hidden ? open() : close(); };
  $("hub-repo-help").onclick = () => { close(); openHubHelp(); };
  document.addEventListener("click", e => { if(!menu.hidden && !menu.contains(e.target)) close(); });
  // Bound to the PICKER, not the document. A page-wide keydown scheme is the kind nobody can
  // discover and everybody trips over; these keys only mean anything while focus is inside this
  // control, which is exactly what a listbox owes the keyboard and no more.
  picker.addEventListener("keydown", e => {
    if(e.key === "Escape"){ close(); btn.focus(); return; }
    if(menu.hidden || !items.length) return;
    if(e.key === "ArrowDown" || e.key === "ArrowUp"){
      e.preventDefault();
      cursor = (cursor + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length;
      mark(); items[cursor].scrollIntoView({block: "nearest"});
    } else if(e.key === "Enter"){
      e.preventDefault(); items[cursor].click();
    }
  });
}

// Whether aGiTrack is actually running on the repository being shown, and in which mode. A
// dashboard that looks identical whether or not anything is being tracked is a dashboard you
// cannot use to answer "is this on?", which is the first thing anyone asks of it.
async function refreshHubState(){
  if(!HUB) return;
  const box = $("hub-state"); if(!box) return;
  let state = null;
  try{ state = await (await fetch("state", {cache:"no-store"})).json(); }catch(e){}
  if(!state || !state.label){ box.hidden = true; return; }
  box.classList.toggle("live", !!state.running);
  box.title = state.detail || "";
  $("hub-state-text").textContent = state.label;
  box.hidden = false;
}

async function initHubBar(){
  if(!HUB) return;
  const bar = $("hubbar"); if(!bar) return;
  const active = $("hub-active"), backtrace = $("hub-backtrace"), views = $("hub-views");
  active.href = hubUrl("active", HUB.slug);
  backtrace.href = hubUrl("backtrace", HUB.slug);
  active.classList.toggle("on", HUB.view === "active");
  backtrace.classList.toggle("on", HUB.view === "backtrace");
  views.hidden = false;
  bar.hidden = false;
  const closeBtn = $("hub-help-close");
  if(closeBtn) closeBtn.onclick = closeHubHelp;
  const modal = $("hub-help");
  if(modal){
    modal.onclick = e => { if(e.target === modal) closeHubHelp(); };
    modal.addEventListener("keydown", e => { if(e.key === "Escape") closeHubHelp(); });
  }
  refreshHubState();
  setInterval(refreshHubState, 15000);
  let repos = [];
  try{ repos = (await (await fetch("/repos", {cache:"no-store"})).json()).repos || []; }catch(e){}
  if(!repos.length){ $("hub-repo-btn").parentElement.hidden = true; return; }
  wireRepoPicker(repos);
}"""

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
        .replace("__UI_HUBBAR_CSS__", HUBBAR_CSS)
        .replace("__UI_HUBBAR_HTML__", HUBBAR_HTML)
        .replace("__UI_HUBBAR_JS__", HUBBAR_JS)
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
