#!/usr/bin/env python3
"""
DemoBank QA Agent
-----------------
Reads a PRD, drives the live web app in a real browser, and writes a
requirement-level QA report (passed / failed / unverified) with screenshots,
reproduction steps, and an estimated token cost.

Usage:
    python qa_agent.py --url https://<demobank-url> --prd ../test-website/PRD.md
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from dotenv import load_dotenv
from google import genai
from google.genai import types
from playwright.sync_api import sync_playwright, Error as PlaywrightError

load_dotenv()

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
# Paid-tier prices in USD per 1M tokens. Check https://ai.google.dev/pricing and update if needed.
PRICE_IN = float(os.getenv("PRICE_INPUT_PER_M", "0.75"))
PRICE_OUT = float(os.getenv("PRICE_OUTPUT_PER_M", "3.75"))
# Free-tier keys are rate limited, so calls are spaced out (seconds between calls).
MIN_GAP = float(os.getenv("MIN_SECONDS_BETWEEN_CALLS", "6.5"))
PAGE_TEXT_LIMIT = 5000
VERDICTS = ("passed", "failed", "unverified")


# ---------------------------------------------------------------- LLM wrapper
class LLM:
    """Every model call goes through here, so tokens and cost are always counted."""

    def __init__(self):
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            sys.exit("GEMINI_API_KEY is missing. Add it to agent/.env")
        self.client = genai.Client(api_key=key)
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self._last = 0.0

    def ask_json(self, prompt, retries=6):
        for attempt in range(retries):
            gap = MIN_GAP - (time.time() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.time()
            try:
                resp = self.client.models.generate_content(
                    model=MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0, response_mime_type="application/json"
                    ),
                )
                self.calls += 1
                usage = resp.usage_metadata
                if usage:
                    self.tokens_in += usage.prompt_token_count or 0
                    self.tokens_out += (usage.candidates_token_count or 0) + (
                        getattr(usage, "thoughts_token_count", 0) or 0
                    )
                return json.loads(resp.text)
            except json.JSONDecodeError:
                print("    ! model returned invalid JSON, retrying")
            except Exception as e:  # rate limits, network blips
                wait = min(60, 5 * 2 ** attempt)
                print(f"    ! LLM error ({str(e)[:100]}), retrying in {wait}s")
                time.sleep(wait)
        raise RuntimeError("LLM call failed after retries")

    def cost(self):
        return self.tokens_in / 1e6 * PRICE_IN + self.tokens_out / 1e6 * PRICE_OUT


def render(template, **values):
    for key, val in values.items():
        template = template.replace("{" + key + "}", str(val))
    return template


# ---------------------------------------------------------------- prompts
PLAN_PROMPT = """You are a senior QA engineer. Read the product requirements document (PRD) below.

1. Split it into atomic, verifiable requirements (one behavior each). Skip purpose,
   out-of-scope and seed-data notes unless they describe user-visible behavior.
2. For each requirement decide whether a browser agent can test it through the UI.
3. For testable ones, write a self-contained test task for a browser agent that starts on
   the app's landing page with a fresh session (it must click "Enter Demo" itself).
   Include the negative and edge cases the requirement implies (for example invalid
   amounts: blank, 0, -5, abc, 10.001, more than the balance). Tell the agent which exact
   values to record before and after actions so the result can be proven. For date-range
   filters, test the boundaries: find a date that has a transaction in the history and use it
   as both the start and the end date. Keep each task
   doable in about 15 browser actions; split bigger ones into separate requirements.
4. Priority: 1 = money movement, validation, security/session; 2 = data display and
   filters; 3 = cosmetic or general UI.

Return JSON only, in this shape:
{"requirements": [{"id": "REQ-01", "section": "...", "requirement": "...",
  "browser_testable": true, "priority": 1, "test_task": "...", "expected": "..."}]}

PRD:
---
{prd}
---"""

STEP_PROMPT = """You are an autonomous QA agent testing a web app against ONE requirement.
Be skeptical: a click or a success message is NOT proof. Verify real outcomes (balances,
history rows, error messages, URLs) and write down the exact values you see.

REQUIREMENT {rid}: {requirement}
TEST TASK: {task}
EXPECTED: {expected}

STEPS SO FAR (oldest first):
{history}

CURRENT PAGE
URL: {url}
Interactive elements (use the number in brackets as "index"):
{elements}

Visible text (may be truncated):
{text}

Choose ONE next action:
- click (index)  - fill (index, value)  - select (index, value = option text)
- press (value, e.g. "Enter")  - goto (value = a path on this same site, e.g. "/dashboard")
- refresh  - back  - wait
- finish (when you can give a verdict)

Verdict rules for finish:
- "passed" only if you directly observed evidence that the requirement holds.
- "failed" if you observed behavior that contradicts it. Give expected vs observed with exact values.
- "unverified" if you could not reliably check it (explain why). Never guess.

Return JSON only:
{"thought": "short reasoning", "observation": "exact relevant values visible now",
 "action": "click|fill|select|press|goto|refresh|back|wait|finish",
 "index": null, "value": null, "verdict": null, "observed": null, "reason": null}"""

# Tags every visible interactive element with a number the model can refer to.
ELEMENTS_JS = r"""
() => {
  document.querySelectorAll('[data-qa-idx]').forEach(e => e.removeAttribute('data-qa-idx'));
  const sel = 'a, button, input, select, textarea, [role="button"], [role="link"], [role="tab"],'
            + '[role="menuitem"], [role="checkbox"], [role="combobox"], [role="option"]';
  const out = [];
  let i = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    if (r.width === 0 || r.height === 0 || st.visibility === 'hidden' || st.display === 'none') continue;
    el.setAttribute('data-qa-idx', String(i));
    let label = el.getAttribute('aria-label') || '';
    if (!label && el.id) {
      const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l) label = l.innerText;
    }
    if (!label) { const l = el.closest('label'); if (l) label = l.innerText; }
    const item = {
      i: i, tag: el.tagName.toLowerCase(), type: el.getAttribute('type') || '',
      label: (label || '').trim().slice(0, 80), text: (el.innerText || '').trim().slice(0, 80),
      placeholder: el.getAttribute('placeholder') || '', href: el.getAttribute('href') || '',
      value: (el.tagName !== 'SELECT' && el.value !== undefined) ? String(el.value) : '',
      disabled: !!el.disabled
    };
    if (el.tagName === 'SELECT') {
      item.options = Array.from(el.options).map(o => o.text.trim()).slice(0, 25);
      item.selected = el.selectedIndex >= 0 ? el.options[el.selectedIndex].text.trim() : '';
    }
    out.push(item);
    i++;
  }
  return out;
}
"""


# ---------------------------------------------------------------- browser helpers
def describe(e):
    return e["label"] or e["text"] or e["placeholder"] or e["href"] or e["tag"]


def format_elements(els):
    lines = []
    for e in els[:150]:
        parts = [f'[{e["i"]}] {e["tag"]}']
        if e["type"]:
            parts.append(f'type={e["type"]}')
        name = describe(e)
        if name != e["tag"]:
            parts.append(f'"{name}"')
        if e["value"]:
            parts.append(f'value="{e["value"]}"')
        if e.get("options"):
            parts.append("options=" + json.dumps(e["options"]))
        if e.get("selected"):
            parts.append(f'selected="{e["selected"]}"')
        if e["type"] == "date":
            parts.append("(fill dates as YYYY-MM-DD)")
        if e["disabled"]:
            parts.append("(disabled)")
        lines.append(" ".join(parts))
    return "\n".join(lines) or "(none)"


def settle(page):
    """Wait for async UI updates (API calls, re-renders) to finish."""
    try:
        page.wait_for_load_state("networkidle", timeout=4000)
    except PlaywrightError:
        pass
    page.wait_for_timeout(400)


def page_text(page):
    try:
        return page.inner_text("body")[:PAGE_TEXT_LIMIT]
    except PlaywrightError:
        return "(page text unavailable)"


def run_action(page, step, els, base_url):
    """Performs one action and returns a human-readable reproduction step."""
    act = str(step.get("action") or "").lower()
    val = step.get("value")
    by_idx = {e["i"]: e for e in els}

    def target():
        try:
            idx = int(step.get("index"))
        except (TypeError, ValueError):
            raise ValueError(f"invalid index {step.get('index')!r}")
        if idx not in by_idx:
            raise ValueError(f"index {idx} is not on the page")
        return page.locator(f'[data-qa-idx="{idx}"]').first, describe(by_idx[idx])

    if act == "click":
        loc, name = target()
        loc.click(timeout=5000)
        return f'Click "{name}"'
    if act == "fill":
        loc, name = target()
        loc.fill("" if val is None else str(val), timeout=5000)
        return f'Enter "{val}" in "{name}"'
    if act == "select":
        loc, name = target()
        try:
            loc.select_option(label=str(val), timeout=5000)
        except PlaywrightError:
            loc.select_option(value=str(val), timeout=5000)
        return f'Select "{val}" in "{name}"'
    if act == "press":
        page.keyboard.press(str(val))
        return f"Press {val}"
    if act == "goto":
        dest = urljoin(base_url, str(val))
        if urlparse(dest).netloc != urlparse(base_url).netloc:
            raise ValueError("navigation outside the target app is not allowed")
        page.goto(dest)
        return f"Navigate directly to {urlparse(dest).path}"
    if act == "refresh":
        page.reload()
        return "Refresh the page"
    if act == "back":
        page.go_back()
        return "Press the browser Back button"
    if act == "wait":
        page.wait_for_timeout(1500)
        return "Wait for the page to update"
    raise ValueError(f"unknown action {act!r}")


# ---------------------------------------------------------------- one test
def run_test(llm, browser, req, base_url, out_dir, max_steps):
    rdir = out_dir / req["id"]
    rdir.mkdir(parents=True, exist_ok=True)
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})  # fresh session
    page = ctx.new_page()
    history, repro, shots = [], [f"Open {base_url}"], []
    result = {
        "verdict": "unverified",
        "observed": "",
        "reason": f"Step budget ({max_steps}) ran out before a verdict was reached.",
    }
    try:
        page.goto(base_url)
        settle(page)
        for n in range(1, max_steps + 1):
            els = page.evaluate(ELEMENTS_JS)
            prompt = render(
                STEP_PROMPT,
                rid=req["id"],
                requirement=req["requirement"],
                task=req["test_task"],
                expected=req.get("expected", ""),
                history="\n".join(history) or "(none yet)",
                url=page.url,
                elements=format_elements(els),
                text=page_text(page),
            )
            step = llm.ask_json(prompt)
            obs = step.get("observation") or ""

            if str(step.get("action") or "").lower() == "finish":
                shot = rdir / f"step_{n:02d}_final.png"
                page.screenshot(path=str(shot), full_page=True)
                shots.append(shot)
                result = {
                    "verdict": str(step.get("verdict") or "unverified").lower().strip(),
                    "observed": step.get("observed") or obs,
                    "reason": step.get("reason") or "",
                }
                break

            try:
                done = run_action(page, step, els, base_url)
                settle(page)
                repro.append(done)
                outcome = "ok"
            except Exception as e:  # recover: tell the model and let it try something else
                done = f'{step.get("action")} (index={step.get("index")}, value={step.get("value")})'
                outcome = "ERROR: " + str(e).splitlines()[0][:200]
            shot = rdir / f"step_{n:02d}.png"
            page.screenshot(path=str(shot))
            shots.append(shot)
            history.append(f"{n}. Saw: {obs} | Did: {done} -> {outcome}")
            print(f"    {n:>2}. {done}  [{outcome[:60]}]")
    except Exception as e:
        result = {"verdict": "unverified", "observed": "", "reason": f"Agent error: {str(e)[:200]}"}
        try:
            shot = rdir / "error.png"
            page.screenshot(path=str(shot))
            shots.append(shot)
        except PlaywrightError:
            pass
    finally:
        ctx.close()

    if result["verdict"] not in VERDICTS:
        result["verdict"] = "unverified"
    return {
        **req,
        **result,
        "repro": repro,
        "screenshots": [s.relative_to(out_dir).as_posix() for s in shots],
    }


# ---------------------------------------------------------------- report
def esc(s):
    return str(s or "").replace("|", "\\|").replace("\n", " ")


def write_report(out_dir, args, reqs, results, llm, started, finished):
    by_id = {r["id"]: r for r in results}
    rows, counts = [], {v: 0 for v in VERDICTS}
    for req in reqs:
        r = by_id.get(req["id"])
        if r:
            verdict, note = r["verdict"], r.get("reason", "")
        elif not req.get("browser_testable"):
            verdict, note = "unverified", "Not testable through the browser UI."
        else:
            verdict, note = "unverified", "Not covered in this run (test budget)."
        counts[verdict] += 1
        rows.append(
            f'| {req["id"]} | {esc(req.get("section"))} | {esc(req["requirement"])} '
            f"| **{verdict.upper()}** | {esc(note)[:160]} |"
        )

    md = [
        "# DemoBank QA Audit Report",
        "",
        f"- **Target:** {args.url}",
        f"- **Specification:** `{args.prd}`",
        f"- **Model:** {MODEL}",
        f"- **Run:** {started:%Y-%m-%d %H:%M} to {finished:%H:%M} "
        f"({(finished - started).seconds // 60} min)",
        "",
        "## Summary",
        "",
        f"{len(reqs)} requirements extracted from the PRD, {len(results)} tested in the browser.",
        "",
        f"| Passed | Failed | Unverified |\n|---|---|---|\n"
        f'| {counts["passed"]} | {counts["failed"]} | {counts["unverified"]} |',
        "",
        "## Requirement results",
        "",
        "| ID | Section | Requirement | Verdict | Notes |",
        "|---|---|---|---|---|",
        *rows,
        "",
    ]

    order = {"failed": 0, "unverified": 1, "passed": 2}
    md += ["## Test details and evidence", ""]
    for r in sorted(results, key=lambda x: order[x["verdict"]]):
        md += [
            f'### {r["id"]}: {r["verdict"].upper()}',
            "",
            f'**Requirement:** {r["requirement"]}',
            "",
            f'**Expected:** {r.get("expected", "")}',
            "",
            f'**Observed:** {r.get("observed", "")}',
            "",
            f'**Reason:** {r.get("reason", "")}',
            "",
            "**Steps to reproduce:**",
            "",
            *[f"{i}. {s}" for i, s in enumerate(r["repro"], 1)],
            "",
        ]
        if r["screenshots"]:
            md += [
                f'![Final state for {r["id"]}]({r["screenshots"][-1]})',
                "",
                "All step screenshots: "
                + " ".join(f"[{i}]({s})" for i, s in enumerate(r["screenshots"], 1)),
                "",
            ]

    md += [
        "## Cost breakdown",
        "",
        "| Item | Value |",
        "|---|---|",
        f"| Model | {MODEL} |",
        f"| LLM calls | {llm.calls} |",
        f"| Input tokens | {llm.tokens_in:,} |",
        f"| Output tokens (incl. thinking) | {llm.tokens_out:,} |",
        f"| Price used (per 1M tokens) | ${PRICE_IN} in / ${PRICE_OUT} out |",
        f"| **Estimated cost at paid rates** | **${llm.cost():.4f}** |",
        "",
        "This run used the Gemini free tier, so the actual charge was $0.00. "
        "The estimate shows what the same run would cost on the paid tier.",
        "",
    ]
    path = out_dir / "report.md"
    path.write_text("\n".join(md), encoding="utf-8")
    return path, counts


# ---------------------------------------------------------------- main
def priority(r):
    try:
        return int(r.get("priority", 3))
    except (TypeError, ValueError):
        return 3


def main():
    ap = argparse.ArgumentParser(description="Autonomous spec-driven QA agent")
    ap.add_argument("--url", required=True, help="URL of the app under test")
    ap.add_argument("--prd", default="../test-website/PRD.md", help="path to the PRD")
    ap.add_argument("--max-tests", type=int, default=10, help="how many requirements to test")
    ap.add_argument("--max-steps", type=int, default=18, help="browser actions per test")
    ap.add_argument("--out", default="runs", help="output folder")
    ap.add_argument("--headless", action="store_true", help="hide the browser window")
    ap.add_argument("--slow-mo", type=int, default=250, help="ms delay per action (for demos)")
    args = ap.parse_args()

    prd = Path(args.prd).read_text(encoding="utf-8")
    started = datetime.now()
    out_dir = Path(args.out) / started.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    llm = LLM()

    print("1/3 Reading the PRD and planning tests...")
    reqs = llm.ask_json(render(PLAN_PROMPT, prd=prd)).get("requirements", [])
    (out_dir / "requirements.json").write_text(json.dumps(reqs, indent=2), encoding="utf-8")
    testable = [r for r in reqs if r.get("browser_testable") and r.get("test_task")]
    selected = sorted(testable, key=priority)[: args.max_tests]
    print(f"    {len(reqs)} requirements found; testing {len(selected)} of them.")

    print("2/3 Testing in the browser...")
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless, slow_mo=args.slow_mo)
        for i, req in enumerate(selected, 1):
            print(f"\n[{i}/{len(selected)}] {req['id']}: {req['requirement'][:90]}")
            res = run_test(llm, browser, req, args.url, out_dir, args.max_steps)
            print(f"    -> {res['verdict'].upper()}: {str(res.get('reason', ''))[:120]}")
            results.append(res)
            (out_dir / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        browser.close()

    print("\n3/3 Writing the report...")
    path, counts = write_report(out_dir, args, reqs, results, llm, started, datetime.now())
    print(f"    Passed {counts['passed']} | Failed {counts['failed']} | Unverified {counts['unverified']}")
    print(f"    {llm.calls} LLM calls, {llm.tokens_in:,} in / {llm.tokens_out:,} out tokens, "
          f"est. ${llm.cost():.4f}")
    print(f"    Report: {path}")


if __name__ == "__main__":
    main()
