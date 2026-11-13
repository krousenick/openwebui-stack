---
name: artifact-design
description: >-
  Build a self-contained HTML page that looks deliberate on any screen:
  explainers, calculators and other interactive tools, dashboards, and
  styled reports. Covers one-file structure, color tokens with dark mode,
  type and spacing scales, responsive layout, accessible controls, and the
  limits of Open WebUI's sandboxed Artifacts panel. Use whenever the answer
  is a web page, an HTML artifact, or an interactive widget. For charts
  also use dataviz; for diagrams, artifact-diagramming.
license: MIT
---

# Artifact Design

A page is worth making when the reader will use it, not just read it.
Build it as one complete HTML file that renders correctly the first time,
on a phone and on a desktop, in light and dark themes.

## When a page is the right answer

| Make a page for | Answer in Markdown for |
|---|---|
| Inputs and live results: calculators, converters, estimators | Prose explanations and advice |
| Exploring data: filters, sorting, toggles, charts | A short list or one table read once |
| Something the user will share or print: a one-pager, a styled report | Code the user will paste elsewhere |
| A visual explainer where layout carries meaning | Anything under a screen of text |

## Where it renders

| You are in | Deliver | Limits |
|---|---|---|
| Open WebUI | One ` ```html ` code block holding the complete document. The Artifacts panel renders it beside the chat; every revision is a new complete block, and the panel keeps a version selector. | Sandboxed iframe: scripts, forms, and downloads work, but there is no same-origin access, so `localStorage`, `sessionStorage`, `indexedDB`, and cookies throw. No access to the chat, the user's session, or a server. External scripts load only from CDNs the user's browser can reach. |
| Claude Code or opencode | Write `<name>.html` in the working directory (or where the user asked), then give the path. Open it with a browser tool if you have one and check the console. | Same rules: the page must stand alone. |

## Structure: one file, complete every time

Start from this skeleton. It defines the design tokens once and switches
them for dark mode:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Loan payment calculator</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #ffffff; --surface: #f6f8fa; --text: #1f2328; --muted: #59636e;
    --border: #d1d9e0; --accent: #0969da; --on-accent: #ffffff;
    --good: #1a7f37; --bad: #cf222e;
    --radius: 10px; --s1: 4px; --s2: 8px; --s3: 12px; --s4: 16px; --s6: 24px; --s8: 32px;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0d1117; --surface: #161b22; --text: #e6edf3; --muted: #9198a1;
      --border: #30363d; --accent: #4493f8; --on-accent: #0d1117; --good: #3fb950; --bad: #f85149;
    }
  }
  *, *::before, *::after { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text); font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  main { max-width: 720px; margin: 0 auto; padding: var(--s6) var(--s4) var(--s8); }
  h1 { font-size: 28px; line-height: 1.2; margin: 0 0 var(--s2); }
  .lede { color: var(--muted); margin: 0 0 var(--s6); }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: var(--s4); }
  .grid { display: grid; gap: var(--s4); grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); }
  label { display: block; font-weight: 600; font-size: 14px; margin-bottom: var(--s1); }
  input { width: 100%; font: inherit; color: inherit; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: var(--s2) var(--s3); }
  input:focus-visible, button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .result { font-size: 32px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .note { color: var(--muted); font-size: 14px; }
  .error { color: var(--bad); font-size: 14px; min-height: 1.5em; }
  @media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }
  @media print { body { background: #fff; color: #000; } .card { border-color: #999; } }
</style>
</head>
<body>
<main>
  <h1>Loan payment calculator</h1>
  <p class="lede">Monthly payment for a fixed-rate loan. Results update as you type.</p>
  <div class="grid">
    <div><label for="amount">Loan amount ($)</label><input id="amount" type="number" min="0" step="1000" value="300000"></div>
    <div><label for="rate">Annual rate (%)</label><input id="rate" type="number" min="0" step="0.05" value="6.5"></div>
    <div><label for="years">Term (years)</label><input id="years" type="number" min="1" max="50" value="30"></div>
  </div>
  <p class="error" id="error" role="alert"></p>
  <section class="card" aria-live="polite">
    <div class="note">Monthly payment</div>
    <div class="result" id="payment">–</div>
    <div class="note" id="total"></div>
  </section>
</main>
<script>
(() => {
  const $ = (id) => document.getElementById(id);
  const money = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  function render() {
    const amount = Number($("amount").value), rate = Number($("rate").value) / 100 / 12, months = Number($("years").value) * 12;
    if (!(amount > 0) || !(months > 0) || rate < 0) {
      $("error").textContent = "Enter a positive amount and term, and a rate of 0 or more.";
      return;
    }
    $("error").textContent = "";
    const payment = rate === 0 ? amount / months : (amount * rate) / (1 - Math.pow(1 + rate, -months));
    $("payment").textContent = money.format(payment);
    $("total").textContent = `${money.format(payment * months - amount)} total interest over ${months} payments`;
  }
  document.querySelectorAll("input").forEach((el) => el.addEventListener("input", render));
  render();
})();
</script>
</body>
</html>
```

## Rules for the file

- Complete document every time: doctype, `lang`, charset, viewport,
  `<title>`. In Open WebUI, never send a fragment or a diff; send the
  whole page again.
- CSS and JavaScript inline. Add a library only when it saves real work
  (charts, maps, 3D), loaded from a CDN with an exact version, e.g.
  `https://cdn.jsdelivr.net/npm/<package>@<x.y.z>/...`; never `@latest`.
- Data goes inline as a JavaScript constant. No `fetch` to APIs unless
  the user asked for it and the API allows browser requests.
- Do not depend on storage. If a convenience needs it, wrap every access
  in `try/catch` and keep working without it.
- Insert user-entered text with `textContent`, never `innerHTML`.
- System font stack; images as inline SVG or CSS. No assets from
  arbitrary hosts.
- Wrap the script in a function so nothing leaks into the global scope.

## Five design decisions

1. **Color.** Tokens on `:root`, redefined under
   `prefers-color-scheme: dark`. One accent, neutrals for everything
   else, semantic colors only for meaning (success, error). Text contrast
   at least 4.5:1; controls and large text at least 3:1.
2. **Type.** A small scale (14, 16, 20, 28, 36 px), line height 1.5 for
   text and 1.2 for headings, lines no longer than about 70 characters,
   two weights (400 and 600 or 700). Numbers in tables and results use
   `font-variant-numeric: tabular-nums`.
3. **Space.** A 4 px scale (4, 8, 12, 16, 24, 32, 48). The same gap for
   the same relationship everywhere; more space between groups than
   within them.
4. **Layout.** One column up to about 720 px for reading. Dashboards use
   CSS grid with `repeat(auto-fit, minmax(240px, 1fr))`. Side padding of
   16 px on phones and no horizontal scrolling at 360 px wide.
5. **Components.** Native elements first (`button`, `input`, `select`,
   `details`, `dialog`). Every input has a visible `<label>`;
   placeholders are not labels. Buttons and touch targets at least 40 px
   high. A visible focus ring on everything focusable. Tables
   right-align numbers and keep row lines light.

## Interaction

- Keep state in variables and re-render from it; update results as the
  user types unless the computation is expensive.
- Validate inputs and explain problems next to them, in words.
- Announce changing results to screen readers (`aria-live="polite"`).
- Everything works with the keyboard; custom controls respond to Enter
  and Space.
- Respect `prefers-reduced-motion`.

## Effort

Match the polish to the use: a quick widget needs clean defaults and
correct behavior; a page the user will share or print also needs a
summary at the top, empty and error states, and print styles.

## Before you deliver

- [ ] Renders at 360 px and 1280 px without horizontal scrolling
- [ ] Readable in both light and dark themes
- [ ] Works when storage is unavailable
- [ ] Every input labeled, focus visible, usable by keyboard
- [ ] Numbers and dates formatted with `Intl.NumberFormat` /
  `Intl.DateTimeFormat`
- [ ] No console errors (check in a browser if you have one)
