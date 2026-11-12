---
name: browser
description: Looks at a running web page and drives it as a user would, with the Chromium on this box, headless, at phone and desktop width. Use when a role needs screenshots of a service or has to walk through it, on the preview or dev server whose URL the lead gave.
---

# Looking at a page, and driving it

Use the URL the lead gave as it is: it is the one this container can
reach. Two widths, the phone first: 390×844 and 1280×900, unless the
repository's CLAUDE.md names its own, such as its mockups' widths; then
use those, in the commands below too. Read every PNG you take; a page can
answer 200 and show nothing.

## Screenshots

```sh
chromium --headless --no-sandbox --disable-gpu --hide-scrollbars \
  --window-size=390,844 --screenshot=/tmp/ui/<page>-phone.png <url>
chromium --headless --no-sandbox --disable-gpu --hide-scrollbars \
  --window-size=1280,900 --screenshot=/tmp/ui/<page>-desktop.png <url>
```

The lead on the workbench has the `browser` MCP tools, which do the same
through a tab; a role has the binary.

## Driving

Playwright for Python through `uv`, on that same Chromium. One script per
walkthrough under `/tmp/ui/`, run with one plain command:

```sh
uv run --with playwright python /tmp/ui/walk.py
```

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 390, "height": 844})
    page.on("console", lambda m: m.type == "error" and print("console:", m.text))
    page.on("requestfailed", lambda r: print("failed:", r.url, r.failure))
    page.goto(url)
    page.get_by_role("button", name="Tallenna").click()
    page.screenshot(path="/tmp/ui/<criterion>-<step>-phone.png")
```

If that Chromium refuses to start, or the machine has none, run `uvx playwright install chromium` once,
which lands in the shared cache, and launch without `executable_path`.
The screenshots above are then `page.screenshot` at each width too.

- Find things as a user does: `get_by_role`, `get_by_label`,
  `get_by_text`, before any CSS selector.
- Screenshot every step that matters, named for the criterion and the
  step, and collect console errors and failed requests.
- Tab through each form once: every control reachable, the focus visible.
- Mark what you enter, with values the user can tell from theirs: the
  service keeps its data.
- Only the service, over HTTP: never its files, and never start, stop or
  rebuild it; that is the `preview` skill's, and the lead's.
