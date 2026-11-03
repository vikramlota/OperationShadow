# Phase 1 — Stabilize and Audit Report (`docs/PHASE1_REPORT.md`)

## 1. Executive Summary
Phase 1 (`phase1-stabilize`) establishes a deterministic, safety-locked baseline for Operation Shadow across all six workstreams in the requested commit order (**A → D → B → C → F → E**). All test suites pass in simulated mode (`SHADOW_HARDWARE_LOCKOUT=true`, `SHADOW_DEFAULT_VEHICLE_MODE=simulated`, `GEMINI_API_KEY` unset), Gemini API calls are bounded by a hard wall-clock deadline (`SHADOW_LLM_PLANNING_TIMEOUT_SEC=10`, `SHADOW_LLM_REPLANNING_TIMEOUT_SEC=8`, `SHADOW_LLM_CONNECT_TIMEOUT_SEC=3`) and a thread-safe circuit breaker (`SHADOW_LLM_BREAKER_COOLDOWN_SEC=30`), fallback plans preserve each mission's custom objective while passing `validate_plan`, personal LAN IPs/usernames/machine paths have been scrubbed, `HAWK01` is explicitly labeled `"simulated / roadmap, not integrated"`, and every rover sensor signal has been audited end-to-end.

---

## 2. Files Changed Per Workstream & Commit Log

| Order | Workstream | Commit Message | Files Created / Modified |
| ---: | --- | --- | --- |
| **1** | **Workstream A — Baseline** | `Workstream A: establish simulated baseline and document test/API verification in docs/BASELINE.md` | `docs/BASELINE.md` *(created)* |
| **2** | **Workstream D — Safety posture** | `Workstream D: align safety posture docs, add startup safety/secret warnings, add SUPERVISED_REAL_MODE.md, and fix start_system.sh` | `docs/SUPERVISED_REAL_MODE.md` *(created)*, `README.md`, `ULTRON_STATUS.md`, `SYSTEM_TEST_RESULTS.md`, `backend/fastapi/main.py`, `backend/tests/test_audit_regressions.py`, `start_system.sh` |
| **3** | **Workstream B — Gemini timeout and fast fallback** | `Workstream B: add bounded Gemini wall-clock timeout, circuit breaker, error classification, PlannerOutcome, and diagnostics CLI` | `ultron/planner/engine.py`, `ultron/config.py`, `ultron/diagnostics.py` *(created)*, `scripts/force_gemini_timeout.py` *(created)*, `tests/test_ultron_planner.py`, `.kiro/specs/ultron-gemini-ssl-timeout/bugfix.md`, `ULTRON_STATUS.md`, `SYSTEM_TEST_RESULTS.md` |
| **4** | **Workstream C — Fallback usable for event-driven missions** | `Workstream C: thread mission objective into fallback plan, add FALLBACK_PLANS.md, and test 3 arbitrary objectives` | `ultron/planner/cache.py`, `ultron/planner/engine.py`, `docs/FALLBACK_PLANS.md` *(created)*, `tests/test_ultron_planner.py`, `.env.example` |
| **5** | **Workstream F — Scrub and freeze** | `Workstream F: scrub personal IPs, usernames, machine paths, and label HAWK as simulated/roadmap` | `SYSTEM_TEST_RESULTS.md`, `ULTRON_STATUS.md`, `SCOUT01_VERIFICATION_CHECKLIST.md`, `FRONTEND_DEBUG_GUIDE.md`, `RUN_MISSION_FROM_FRONTEND.md`, `docs/BUG_AUDIT_AND_OPTIMIZATION_REPORT.md`, `enable_scout_ssh.sh`, `test_scout_ssh.sh`, `README.md`, `AGENTS.md`, `configs/vehicles.json`, `frontend/src/api/client.ts`, `frontend/src/components/FloorMap.tsx`, `frontend/src/__tests__/App.test.tsx`, `backend/fastapi/main.py`, `backend/fastapi/api/map.py`, `backend/tests/test_milestone1_scaffold.py` |
| **6** | **Workstream E — Signal audit** | `Workstream E: add end-to-end signal audit in docs/SIGNAL_AUDIT.md and read-only scripts/signal_probe.py` | `docs/SIGNAL_AUDIT.md` *(created)*, `scripts/signal_probe.py` *(created)* |

---

## 3. Test Results Before and After Phase 1

All suites were executed with `SHADOW_DEFAULT_VEHICLE_MODE=simulated`, `SHADOW_HARDWARE_LOCKOUT=true`, and `GEMINI_API_KEY` unset:

| Suite | Before Phase 1 | After Phase 1 | Delta / Notes |
| --- | --- | --- | --- |
| `make test-architecture` | 2 passed, 0 failed (`0.13s`) | **2 passed, 0 failed** (`0.13s`) | TRD §26 repository layout & segregation preserved |
| `make test-ultron` | 15 passed, 0 failed (`0.33s`) | **18 passed, 0 failed** (`1.62s`) | Added 3 new tests in `tests/test_ultron_planner.py` covering wall-clock timeout, LangGraph validation-loop circuit breaker, retry gating, error classification, and 3 arbitrary fallback objectives |
| `make test-backend` | 49 passed, 0 failed (`10.00s`) | **50 passed, 0 failed** (`10.10s`) | Added `test_startup_safety_and_secret_warnings_logged` in `backend/tests/test_audit_regressions.py` + updated `/scout/status` fixture IP in `test_milestone1_scaffold.py` |
| `make test-frontend` | `tsc` + 16 passed, 0 failed (`2.27s`) | **`tsc` + 16 passed, 0 failed** (`2.41s`) | Updated fixture IPs to RFC 5737 `192.0.2.41` and HAWK geofence label in `App.test.tsx` |
| `scripts/force_gemini_timeout.py` | *(N/A)* | **4/4 assertions PASS** (`1.8s`) | Proves single-call timeout fallback (`<= budget + 1s`), 3-retry LangGraph validation loop bounded to 1 budget via breaker, `reset_breaker()`, and no-retry on HTTP 401 |

---

## 4. Key Engineering Decisions for Review

1. **Fast Bounded Fallback Over Long SSL Retries (`Workstream B`)**:
   - `.kiro/specs/ultron-gemini-ssl-timeout/bugfix.md` originally proposed a 45s SSL handshake timeout with 1s/2s/4s exponential backoff retries (which could block a single LangGraph 3-attempt validation loop for over 3 minutes).
   - We marked `bugfix.md` as superseded by the Phase 1 decision: `SHADOW_LLM_PLANNING_TIMEOUT_SEC=10` (default 10s), `SHADOW_LLM_REPLANNING_TIMEOUT_SEC=8` (default 8s), `SHADOW_LLM_CONNECT_TIMEOUT_SEC=3` (default 3s, passed via `httpx.Timeout(timeout_sec, connect=connect_timeout_sec)` in `google-genai`'s `HttpOptions.client_args`), at most **1 retry** (only for `dns`, `tcp_connect`, `tls_handshake` when `>= 40%` of the wall-clock budget remains), and a module-level thread-safe circuit breaker (`SHADOW_LLM_BREAKER_COOLDOWN_SEC=30`).
2. **Abandoning Hung SDK Worker Threads (`Workstream B`)**:
   - Because `google-genai`'s synchronous `client.models.generate_content` call inside a thread cannot be interrupted mid-C-socket handshake, `_invoke_gemini_with_outcome` submits `_call_gemini_sdk` to a single-worker `ThreadPoolExecutor` and calls `executor.shutdown(wait=False, cancel_futures=True)` immediately on `FuturesTimeoutError`, returning `build_evidence_grounded_plan` within milliseconds of the deadline.
3. **Preserving `MissionPlan` Schema (`Workstream B & C`)**:
   - Because `MissionPlan` is passed directly as `response_schema=MissionPlan` to Gemini, no fields were added to `MissionPlan`. Instead, `PlannerOutcome(source, reason, elapsed_ms)` is stored in thread-safe module state and exposed via `get_last_planner_outcome()`.
4. **Status of `operation-shadow-critical-review.patch` (`Workstream F`)**:
   - Inspection confirmed that all code and test changes inside `operation-shadow-critical-review.patch` (such as `backend/tests/test_audit_regressions.py` and `rosbridge_client.py` hardening) are **already committed** in `HEAD`.
   - Because `operation-shadow-critical-review.patch` is no longer needed and contains the legacy LAN IP `10.241.171.41` on line 351, **we propose deleting it** (`git rm operation-shadow-critical-review.patch`). Per your instructions, we have left the file untouched awaiting your confirmation.
5. **Git History & Local `.env` Audit Commands (`Workstream F`)**:
   - Confirmed `.gitignore` excludes `.env`, `.env.*` (except `!.env.example`), and `*.local`, and `git ls-files "*env*"` tracks only `.env.example` and `frontend/src/vite-env.d.ts`.
   - Note that untracked local `.env` and `ultron/.env` files exist on disk in this workspace (not tracked by git); check them locally before running live services.
   - Run the following commands locally to inspect git commit history for past username or `.env` commits:
     ```bash
     git log --all -p -S"<ROVER_USER>"
     git log --all -- .env
     ```
   - If any API key (`GEMINI_API_KEY`, `ELEVENLABS_API_KEY`) or `SHADOW_VEHICLE_SHARED_SECRET` was ever committed in git history, **rotate the keys immediately** rather than relying solely on history rewrites.

---

## 5. Items Not Verified in Automated Sandbox (Requires Human / Physical Rover)

1. **Physical Rover Signal Column in `docs/SIGNAL_AUDIT.md`**:
   - The `Real-rover result` column in `docs/SIGNAL_AUDIT.md` is intentionally left blank for the human operator to populate by running:
     ```bash
     python3 scripts/signal_probe.py --url http://127.0.0.1:8000 --duration 10
     ```
     while `SCOUT01` is publishing over ROSBridge.
2. **Live Venue Wi-Fi TLS Handshake Timing**:
   - In the offline sandbox environment, `python3 -m ultron.diagnostics` verified clean DNS failure classification (`dns`). Run `python3 -m ultron.diagnostics` on the venue Wi-Fi to measure live DNS, TCP 443, and TLS 1.3 handshake latencies to `generativelanguage.googleapis.com`.

---

## 6. Definition of Done Checklist

- [x] **Tests pass in simulated mode**: `make test-architecture` (2/2), `make test-ultron` (18/18), `make test-backend` (50/50), and `make test-frontend` (`tsc` + 16/16) all pass with `SHADOW_DEFAULT_VEHICLE_MODE=simulated`, `SHADOW_HARDWARE_LOCKOUT=true`, and `GEMINI_API_KEY` unset (documented in `docs/BASELINE.md`).
- [x] **Forced Gemini timeout falls back within ~10s, proven by script and tests**: `scripts/force_gemini_timeout.py` and `tests/test_ultron_planner.py` prove single-call fallback within `budget + 1s`, full `build_ultron_graph().invoke(...)` validation-retry loop bounded to a single budget via the circuit breaker, `reset_breaker()` recovery, and HTTP 401 `http_401_403` no-retry classification.
- [x] **Fallback plans carry the mission's own objective and pass the validator**: `get_fallback_plan(mission_id, objective=None)` and `build_evidence_grounded_plan(world)` preserve `world.mission.objective`, retain `"USED CACHED PLAN"` in `rationale`, and pass `validate_plan` across 3 arbitrary objectives (`docs/FALLBACK_PLANS.md` + `tests/test_ultron_planner.py`).
- [x] **Docs match actual safety defaults, and a supervised-real-mode checklist exists**: `README.md`, `ULTRON_STATUS.md`, and `SYSTEM_TEST_RESULTS.md` describe the actual safe defaults (`SHADOW_HARDWARE_LOCKOUT=true`, `SHADOW_DEFAULT_VEHICLE_MODE=simulated`), `main.py` logs structured `WARNING`s at startup for unsafe posture or placeholder secrets on non-loopback hosts, and `docs/SUPERVISED_REAL_MODE.md` documents the human-verified preconditions and USB-loss firmware failsafe warning.
- [x] **Signal audit table filled in, with real-rover column ready for the human**: `docs/SIGNAL_AUDIT.md` documents all 8 signals with `file:line` producers, transports, ingest points, code reachability evidence, simulator status, smallest fixes for blocked signals, and 2–3 real trigger recommendations, paired with read-only `scripts/signal_probe.py`.
- [x] **No usernames, LAN IPs, or machine paths remain; HAWK labeled**: All tracked docs, scripts, configs, and test fixtures are scrubbed (`<SCOUT_IP>`, `<ROVER_USER>`, `<LAPTOP_IP>`, `<REPO_ROOT>`, `192.0.2.41`), and `HAWK01` is labeled `"simulated / roadmap, not integrated"`.
