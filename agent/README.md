# DemoBank QA Agent

An autonomous, specification-driven QA agent. It reads a written PRD, drives the live DemoBank web app in a real browser, and reports whether each requirement **passed**, **failed**, or is **unverified**, with screenshots, observed values, reproduction steps, and the token cost of the run.

## Architecture

The agent is a single Python file (`qa_agent.py`) that runs in three phases:

```
PRD.md ──▶ 1. Planner (Gemini) ──▶ requirements.json
                                        │
                                        ▼
            2. Browser loop, one fresh session per requirement:
               observe page (elements + visible text)
                 ──▶ Gemini picks one action (click / fill / select / goto / refresh ...)
                 ──▶ Playwright performs it, waits for the UI to settle, takes a screenshot
                 ──▶ repeat until Gemini gives a verdict or the step budget runs out
                                        │
                                        ▼
            3. Reporter ──▶ report.md + screenshots + results.json + cost table
```

**1. Planner.** One LLM call turns the PRD into atomic requirements, each with an ID, a priority, a test task covering positive, negative, and edge cases, and the expected result. The highest-priority requirements (money movement, validation, session security) are tested first.

**2. Browser loop.** The agent doesn't use hardcoded selectors. On each step it gives the model a numbered list of the page's visible interactive elements and its visible text. The model chooses the next action and records the exact values it sees, such as balances before and after a transfer. If an action fails, the error goes back to the model so it can recover. The prompt tells the model that a click or success message is not proof, and that it must mark a requirement unverified rather than guess.

**3. Reporter.** Writes a Markdown report with a requirement-level results table, details for each tested requirement (expected, observed, steps to reproduce, screenshots), and a cost breakdown. Requirements that weren't tested are listed as unverified with the reason.

**Safety.** Navigation is restricted to the target app's own domain. The agent uses only the synthetic demo data and sends no high-volume traffic.

## Requirements

- macOS, Linux, or Windows
- Python 3.9+
- A Gemini API key (the free tier works): https://aistudio.google.com

## Setup

```bash
cd agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Create `agent/.env` (it is gitignored, so never commit it):

```
GEMINI_API_KEY=your_key_here
```

Optional settings in `.env`: `GEMINI_MODEL`, `PRICE_INPUT_PER_M`, `PRICE_OUTPUT_PER_M`, `MIN_SECONDS_BETWEEN_CALLS`.

## Run

```bash
python qa_agent.py --url <DEMOBANK_URL> --prd ../test-website/PRD.md
```

| Option | Default | Meaning |
|---|---|---|
| `--max-tests` | 10 | Number of requirements to test |
| `--max-steps` | 18 | Browser actions allowed per test |
| `--headless` | off | Hide the browser window |
| `--slow-mo` | 250 | Delay per action in ms, so runs are easy to follow on video |

## Output

Each run creates `runs/<timestamp>/` containing:

- `report.md`: the QA audit report (results, evidence, reproduction steps, cost)
- `requirements.json`: all requirements extracted from the PRD
- `results.json`: raw results for each tested requirement
- `REQ-xx/step_NN.png`: a screenshot after every browser action

## Cost

Every model call goes through one wrapper that counts input and output tokens. The report multiplies those counts by Gemini's paid-tier prices (configurable in `.env`) to estimate the cost of the run. Runs on the free tier cost $0. Calls are spaced a few seconds apart to stay within free-tier rate limits.

## Known limitations

- Only the top `--max-tests` requirements are tested in one run. The rest are reported as unverified with the reason.
- Verdicts are the model's judgment based on what it observed on the page, so false positives or negatives are possible. Screenshots and recorded values are included so each verdict can be checked.
- Results can vary between runs because the browser actions are chosen by the model.
- Tests change application state (for example, transfers change balances). On a shared hosted instance, the data can't be reset between tests.
- Only the 1280 × 800 desktop viewport is tested. Narrower widths are not.
- Free-tier rate limits make a full run take roughly 10–20 minutes.
