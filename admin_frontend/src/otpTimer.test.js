import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  EMPTY_STAGE, canResend, canVerify, createDeadlineTicker, createSubmitGuard, deadlineFromRetryAfter, deadlinesFromIssue, deriveStageView,
  formatOtpClock, isStageVerified, otpEntryHint, remainingSeconds, resendLabel, verifiedDeadline,
} from "./otpTimer.js";
import { cleanOtpInput, describeFlowError } from "./passwordFlow.js";

const source = (name) => readFileSync(new URL(name, import.meta.url), "utf8");

// A fake clock plus fake setInterval / clearInterval so the timer lifecycle can be exercised deterministically.
function fakeTimers(startMs = 1_000_000) {
  const state = { now: startMs, nextId: 1, intervals: new Map(), cleared: [] };
  return {
    state,
    now: () => state.now,
    setIntervalFn: (fn) => { const id = state.nextId++; state.intervals.set(id, fn); return id; },
    clearIntervalFn: (id) => { state.cleared.push(id); state.intervals.delete(id); },
    advance(seconds) {
      state.now += seconds * 1000;
      for (const fn of [...state.intervals.values()]) fn();
    },
    tickOnly(fn) { fn(); },
  };
}

test("expiry clock counts down as 04:59, 04:58 and never below 00:00", () => {
  assert.equal(formatOtpClock(300), "05:00");
  assert.equal(formatOtpClock(299), "04:59");
  assert.equal(formatOtpClock(298), "04:58");
  assert.equal(formatOtpClock(65), "01:05");
  assert.equal(formatOtpClock(0), "00:00");
  assert.equal(formatOtpClock(-4), "00:00");
  assert.equal(formatOtpClock(298.2), "04:59");
  assert.equal(formatOtpClock(undefined), "00:00");
});

test("remaining seconds come from an absolute deadline", () => {
  assert.equal(remainingSeconds(0, 5000), 0);
  assert.equal(remainingSeconds(5000 + 299_000, 5000), 299);
  assert.equal(remainingSeconds(5000 + 298_001, 5000), 299);
  assert.equal(remainingSeconds(4000, 5000), 0);
});

test("timers start only from a backend-issued response and use its values", () => {
  const issued = deadlinesFromIssue({ success: true, expires_in: 300, resend_after: 120 }, 1000);
  assert.deepEqual(issued, { expiresAt: 301_000, resendAt: 121_000 });
  // nothing reported -> nothing started (no invented defaults)
  assert.deepEqual(deadlinesFromIssue({ success: true }, 1000), { expiresAt: 0, resendAt: 0 });
  assert.deepEqual(deadlinesFromIssue(undefined, 1000), { expiresAt: 0, resendAt: 0 });
  assert.equal(deadlineFromRetryAfter(42, 1000), 43_000);
  assert.equal(deadlineFromRetryAfter(0, 1000), 0);
  assert.equal(deadlineFromRetryAfter(undefined, 1000), 0);
  assert.equal(verifiedDeadline({ verified_expires_in: 600 }, 1000), 601_000);
  assert.equal(verifiedDeadline({}, 1000), 0);
});

test("resend button is disabled for the full backend-provided 120-second cooldown, then re-enables", () => {
  const { resendAt } = deadlinesFromIssue({ success: true, expires_in: 300, resend_after: 120 }, 0);
  const remainingAt = (elapsedSeconds) => remainingSeconds(resendAt, elapsedSeconds * 1000);
  for (const elapsed of [0, 1, 60, 119]) {
    assert.equal(canResend({ busy: false, resendRemaining: remainingAt(elapsed) }), false, "still cooling down after " + elapsed + "s");
  }
  assert.equal(remainingAt(0), 120);
  assert.equal(canResend({ busy: false, resendRemaining: remainingAt(120) }), true);
  assert.equal(canResend({ busy: true, resendRemaining: 0 }), false);
  assert.equal(resendLabel({ issued: true, resendRemaining: remainingAt(0), firstLabel: "Send" }), "Resend code in 02:00");
  assert.equal(resendLabel({ issued: true, resendRemaining: remainingAt(119), firstLabel: "Send" }), "Resend code in 00:01");
  assert.equal(resendLabel({ issued: true, resendRemaining: 0, firstLabel: "Send" }), "Resend code");
  assert.equal(resendLabel({ issued: false, resendRemaining: 0, firstLabel: "Send code to current email" }), "Send code to current email");
});

test("code entry and verification are never blocked by the resend timer", () => {
  // canVerify has no resend input at all: only request state, stage state, expiry and code length matter.
  assert.equal(canVerify({ otp: "123456", busy: false, issued: true, verified: false, expired: false }), true);
  assert.equal(canVerify({ otp: "12345", busy: false, issued: true, verified: false, expired: false }), false);
  assert.equal(canVerify({ otp: "123456", busy: true, issued: true, verified: false, expired: false }), false);
  assert.equal(canVerify({ otp: "123456", busy: false, issued: true, verified: true, expired: false }), false);
  assert.equal(canVerify({ otp: "123456", busy: false, issued: true, verified: false, expired: true }), false);
  assert.equal(canVerify({ otp: "123456", busy: false, issued: false, verified: false, expired: false }), false);
});

test("a verified stage counts only while its save window is open", () => {
  assert.equal(isStageVerified({ verified: true, verifiedUntil: 5000 }, 10), true);
  assert.equal(isStageVerified({ verified: true, verifiedUntil: 5000 }, 0), false);
  assert.equal(isStageVerified({ verified: true, verifiedUntil: 0 }, 0), true);
  assert.equal(isStageVerified(EMPTY_STAGE, 100), false);
});

test("ticker keeps one interval, ticks every second from the clock and stops at zero", () => {
  const t = fakeTimers();
  const seen = [];
  const ticker = createDeadlineTicker({ onTick: (n) => seen.push(n), now: t.now, setIntervalFn: t.setIntervalFn, clearIntervalFn: t.clearIntervalFn });
  ticker.start(t.state.now + 3000);
  assert.equal(t.state.intervals.size, 1);
  assert.equal(ticker.active, true);
  t.advance(1); t.advance(1); t.advance(1);
  assert.deepEqual(seen, [3, 2, 1, 0]);
  assert.equal(t.state.intervals.size, 0, "interval is cleared once the countdown reaches zero");
  assert.equal(ticker.active, false);
});

test("ticker never drifts: a throttled tab catches up from the clock", () => {
  const t = fakeTimers();
  const seen = [];
  const ticker = createDeadlineTicker({ onTick: (n) => seen.push(n), now: t.now, setIntervalFn: t.setIntervalFn, clearIntervalFn: t.clearIntervalFn });
  ticker.start(t.state.now + 300_000);
  t.advance(45); // one late tick after 45 seconds
  assert.equal(seen.at(-1), 255);
});

test("restarting replaces the interval (no duplicates) and stale callbacks are ignored", () => {
  const t = fakeTimers();
  const seen = [];
  const ticker = createDeadlineTicker({ onTick: (n) => seen.push(n), now: t.now, setIntervalFn: t.setIntervalFn, clearIntervalFn: t.clearIntervalFn });
  ticker.start(t.state.now + 60_000);
  const [staleCallback] = [...t.state.intervals.values()];
  ticker.start(t.state.now + 300_000);
  assert.equal(t.state.intervals.size, 1, "only one interval is alive after a restart");
  assert.equal(t.state.cleared.length, 1);
  seen.length = 0;
  staleCallback(); // a tick that was already queued for the replaced countdown
  assert.deepEqual(seen, [], "stale callback must not update state");
});

test("stop (unmount) clears the interval and later ticks do nothing", () => {
  const t = fakeTimers();
  const seen = [];
  const ticker = createDeadlineTicker({ onTick: (n) => seen.push(n), now: t.now, setIntervalFn: t.setIntervalFn, clearIntervalFn: t.clearIntervalFn });
  ticker.start(t.state.now + 60_000);
  const [callback] = [...t.state.intervals.values()];
  ticker.stop();
  assert.equal(t.state.intervals.size, 0);
  assert.equal(ticker.active, false);
  seen.length = 0;
  callback();
  assert.deepEqual(seen, []);
  ticker.stop(); // idempotent
});

test("an already elapsed or missing deadline reports zero without starting an interval", () => {
  const t = fakeTimers();
  const seen = [];
  const ticker = createDeadlineTicker({ onTick: (n) => seen.push(n), now: t.now, setIntervalFn: t.setIntervalFn, clearIntervalFn: t.clearIntervalFn });
  ticker.start(0);
  ticker.start(t.state.now - 1000);
  assert.deepEqual(seen, [0, 0]);
  assert.equal(t.state.intervals.size, 0);
});

test("duplicate clicks are ignored while a request is in flight, and the guard always unlocks", async () => {
  const guard = createSubmitGuard();
  let calls = 0;
  let release;
  const pending = guard.run(() => new Promise((resolve) => { calls += 1; release = resolve; }));
  assert.equal(guard.locked, true);
  assert.equal(await guard.run(async () => { calls += 1; }), undefined, "second click is a no-op");
  release("done");
  assert.equal(await pending, "done");
  assert.equal(calls, 1);
  assert.equal(guard.locked, false);
});

test("an API failure releases the guard so the user can retry", async () => {
  const guard = createSubmitGuard();
  await assert.rejects(() => guard.run(async () => { throw new Error("network"); }), /network/);
  assert.equal(guard.locked, false);
  assert.equal(await guard.run(async () => "ok"), "ok");
});

test("the profile component wires backend values to the timers without arbitrary delays", () => {
  const component = source("AccountIdentitySettings.jsx");
  assert.match(component, /deadlinesFromIssue\(result\.data\)/, "countdowns start from the issue response");
  assert.match(component, /deadlineFromRetryAfter\(result\.failure\.retryAfter\)/, "429 retry_after drives the resend cooldown");
  assert.match(component, /verifiedDeadline\(result\.data\)/);
  assert.match(component, /createSubmitGuard/);
  assert.match(component, /mounted\.current/, "state is not updated after unmount");
  assert.doesNotMatch(component, /setTimeout|setInterval/, "no hand-rolled delays or intervals in the component");
  assert.doesNotMatch(component, /disabled=\{[^}]*resendIn[^}]*\}[^>]*onClick=\{\(\) => verify/);
  // verify buttons depend on canVerify (which has no resend input); resend buttons depend on canResend.
  assert.match(component, /const currentCanVerify = canVerify\(/);
  assert.match(component, /const newCanVerify = currentView\.verified && canVerify\(/);
  assert.match(component, /canResend\(\{ busy, resendRemaining: currentView\.resendIn \}\)/);
});

test("changing the email or username clears the matching verification state", () => {
  const component = source("AccountIdentitySettings.jsx");
  assert.match(component, /const changeEmail = [\s\S]*setCurrentStage\(EMPTY_STAGE\); setNewStage\(EMPTY_STAGE\)/);
  assert.match(component, /const changeUsername = [\s\S]*setUsernameStage\(EMPTY_STAGE\)/);
});

test("save stays disabled until both stages verified and success is shown only from the backend response", () => {
  const component = source("AccountIdentitySettings.jsx");
  assert.match(component, /disabled=\{busy \|\| !currentView\.verified \|\| !newView\.verified\}>Save new email address/);
  assert.match(component, /result\.ok && result\.data && result\.data\.success/);
  assert.match(component, /verification_required/);
  assert.match(component, /verification_expired/);
  assert.match(component, /current_email_not_verified/);
});

test("the hook cleans its interval on unmount", () => {
  const hook = source("useDeadlineCountdown.js");
  assert.match(hook, /return \(\) => ticker\.stop\(\)/);
  assert.match(hook, /\[deadline\]/);
});

test("stage view: a fresh code is neither expired nor verified, an unanswered one expires, nothing issued shows nothing", () => {
  const issued = { ...EMPTY_STAGE, issued: true };
  assert.deepEqual(deriveStageView(issued, 299, 0), { verified: false, lapsed: false, expired: false });
  assert.deepEqual(deriveStageView(issued, 0, 0), { verified: false, lapsed: false, expired: true });
  assert.deepEqual(deriveStageView(EMPTY_STAGE, 0, 0), { verified: false, lapsed: false, expired: false });
});

test("stage view: verification lapses cleanly once its save window has run out", () => {
  const verified = { issued: true, verified: true, verifiedUntil: 600_000 };
  assert.deepEqual(deriveStageView(verified, 0, 120), { verified: true, lapsed: false, expired: false });
  const lapsed = deriveStageView(verified, 0, 0);
  assert.equal(lapsed.verified, false);
  assert.equal(lapsed.lapsed, true, "the screen must stop treating a lapsed stage as verified, so Save is disabled");
});

test("regression: a stage verified near the end of its five-minute code window is still verified minutes later", () => {
  // The server answers a correct code with verified_expires_in (600s). The original 300s code expiry must not unverify it.
  const issuedAt = 0;
  const { expiresAt } = deadlinesFromIssue({ expires_in: 300, resend_after: 60 }, issuedAt);
  const verifiedAt = 171_000; // verified 2:51 after issue, as in the failing session
  const stage = { issued: true, expiresAt, verified: true, verifiedUntil: verifiedDeadline({ verified_expires_in: 600 }, verifiedAt) };
  const now = 39_000 + expiresAt; // 39 seconds after the original code expiry, when the second stage is finished
  const view = deriveStageView(stage, remainingSeconds(stage.expiresAt, now), remainingSeconds(stage.verifiedUntil, now));
  assert.deepEqual(view, { verified: true, lapsed: false, expired: false });
});

test("a 429 cooldown response drives only the resend timer; entering and verifying a code stays possible", () => {
  const failure = describeFlowError({ response: { status: 429, data: { success: false, code: "resend_cooldown", message: "Please wait before requesting another code.", retry_after: 42 } } }, "fallback");
  assert.equal(failure.code, "resend_cooldown");
  assert.equal(failure.retryAfter, 42);
  const resendAt = deadlineFromRetryAfter(failure.retryAfter, 1000);
  assert.equal(canResend({ busy: false, resendRemaining: remainingSeconds(resendAt, 1000) }), false);
  assert.equal(canResend({ busy: false, resendRemaining: remainingSeconds(resendAt, 1000 + 42_000) }), true);
  assert.equal(canVerify({ otp: "123456", busy: false, issued: true, verified: false, expired: false }), true);
});

test("the hourly limit and failed delivery keep the server message and invent no cooldown", () => {
  const limited = describeFlowError({ response: { status: 429, data: { success: false, code: "rate_limited", message: "Too many verification requests. Try again later." } } }, "fallback");
  assert.equal(limited.message, "Too many verification requests. Try again later.");
  assert.equal(limited.retryAfter, 0);
  assert.equal(deadlineFromRetryAfter(limited.retryAfter, 1000), 0);
  const mail = describeFlowError({ response: { status: 503, data: { success: false, code: "email_delivery_failed", message: "The verification email could not be sent. Check SMTP configuration and try again." } } }, "fallback");
  assert.equal(mail.code, "email_delivery_failed");
  assert.equal(mail.retryAfter, 0);
  assert.equal(describeFlowError({ message: "Network Error" }, "fallback").code, "network");
});

test("an expired or unavailable verification response leaves the user a way forward", () => {
  const expired = describeFlowError({ response: { status: 400, data: { success: false, code: "verification_expired", message: "Verification expired. Please start again and request new codes." } } }, "fallback");
  assert.equal(expired.code, "verification_expired");
  assert.match(expired.message, /request new codes/);
  const component = source("AccountIdentitySettings.jsx");
  assert.match(component, /Request a new code and verify again/);
  assert.match(component, /This code has expired\. Request a new code to continue/);
});

test("a five-digit code can never be submitted; six digits enable Verify at once, even mid-cooldown", () => {
  const stage = { issued: true, verified: false, expired: false, busy: false };
  assert.equal(canVerify({ ...stage, otp: "12345" }), false);
  assert.equal(canVerify({ ...stage, otp: "" }), false);
  assert.equal(canVerify({ ...stage, otp: "123456" }), true);
  const { resendAt } = deadlinesFromIssue({ expires_in: 300, resend_after: 120 }, 0);
  assert.equal(remainingSeconds(resendAt, 21_000) > 0, true, "the resend cooldown is still running");
  assert.equal(canVerify({ ...stage, otp: "123456" }), true, "and Verify does not look at it");
});

test("leading zeros are preserved: the code stays text and is never converted to a number", () => {
  assert.equal(cleanOtpInput("012345"), "012345");
  assert.equal(cleanOtpInput("000042"), "000042");
  assert.equal(cleanOtpInput(" 0 1-2a3 4 5 "), "012345");
  assert.equal(cleanOtpInput("0123456789"), "012345", "never more than six digits");
  assert.equal(canVerify({ otp: cleanOtpInput("012345"), busy: false, issued: true, verified: false, expired: false }), true);
  const component = source("AccountIdentitySettings.jsx");
  assert.doesNotMatch(component, /(Number|parseInt|parseFloat)\([^)]*[Oo]tp/, "the OTP is never converted");
  assert.match(component, /api\.post\("profile\/email\/verify-otp", \{ email, otp, stage: kind \}\)/);
  assert.match(component, /api\.post\("profile\/username\/verify-otp", \{ username, otp: usernameOtp \}\)/);
});

test("a partly typed code gets an inline hint, a complete, finished or unissued one does not", () => {
  const base = { issued: true, verified: false, expired: false };
  assert.equal(otpEntryHint({ ...base, otp: "12345" }), "Enter all 6 digits of the code (5 of 6 entered).");
  assert.equal(otpEntryHint({ ...base, otp: "1" }), "Enter all 6 digits of the code (1 of 6 entered).");
  assert.equal(otpEntryHint({ ...base, otp: "" }), "");
  assert.equal(otpEntryHint({ ...base, otp: "123456" }), "");
  assert.equal(otpEntryHint({ ...base, otp: "123", issued: false }), "");
  assert.equal(otpEntryHint({ ...base, otp: "123", verified: true }), "");
  assert.equal(otpEntryHint({ ...base, otp: "123", expired: true }), "");
});

test("both countdowns come from one issue response: 05:00 and 02:00, a constant 180 s apart", () => {
  const issuedAt = 10_000;
  const { expiresAt, resendAt } = deadlinesFromIssue({ success: true, expires_in: 300, resend_after: 120 }, issuedAt);
  assert.equal(expiresAt - resendAt, 180_000);
  for (const [elapsed, expiry, resend] of [[0, "05:00", "02:00"], [1, "04:59", "01:59"], [21, "04:39", "01:39"], [119, "03:01", "00:01"]]) {
    const now = issuedAt + elapsed * 1000;
    assert.equal(formatOtpClock(remainingSeconds(expiresAt, now)), expiry);
    assert.equal(formatOtpClock(remainingSeconds(resendAt, now)), resend);
    assert.equal(remainingSeconds(expiresAt, now) - remainingSeconds(resendAt, now), 180);
  }
  assert.equal(remainingSeconds(resendAt, issuedAt + 120_000), 0);
  assert.equal(remainingSeconds(expiresAt, issuedAt + 120_000), 180, "the code is still valid after the cooldown ends");
});

test("the screen shows exactly what the server returned: the old 60 s response gives 04:21 / 00:21, the current 120 s one 04:21 / 01:21", () => {
  const now = 39_000; // 39 seconds after issue
  const old = deadlinesFromIssue({ expires_in: 300, resend_after: 60 }, 0);
  assert.deepEqual([formatOtpClock(remainingSeconds(old.expiresAt, now)), formatOtpClock(remainingSeconds(old.resendAt, now))], ["04:21", "00:21"]);
  const current = deadlinesFromIssue({ expires_in: 300, resend_after: 120 }, 0);
  assert.deepEqual([formatOtpClock(remainingSeconds(current.expiresAt, now)), formatOtpClock(remainingSeconds(current.resendAt, now))], ["04:21", "01:21"]);
});

test("the first Send is never gated by a timer and starts the request immediately", () => {
  assert.equal(EMPTY_STAGE.resendAt, 0);
  assert.equal(canResend({ busy: false, resendRemaining: remainingSeconds(EMPTY_STAGE.resendAt) }), true);
  assert.equal(resendLabel({ issued: false, resendRemaining: 0, firstLabel: "Send code to current email" }), "Send code to current email");
  const component = source("AccountIdentitySettings.jsx");
  assert.doesNotMatch(component, /setTimeout|setInterval|sleep|delay/i, "no frontend delay of any kind before or after a request");
  assert.match(component, /const result = await run\(\(\) => api\.post\(endpoint, body\)\);\s*if \(!result\) return;/, "the request is the first thing issueCode does");
  const startOfIssue = component.indexOf("const issueCode");
  assert.ok(startOfIssue > 0);
  assert.ok(component.indexOf("setStage({ ...EMPTY_STAGE, issued: true, ...deadlinesFromIssue(result.data) })") > component.indexOf("api.post(endpoint, body)"), "timers start only after the response");
});

test("each verify button reads its own code and stage; Verify never reads the resend countdown", () => {
  const component = source("AccountIdentitySettings.jsx");
  assert.match(component, /const otp = kind === "current" \? currentOtp : newOtp;/);
  assert.match(component, /const view = kind === "current" \? currentView : newView;/);
  for (const name of ["usernameCanVerify", "currentCanVerify", "newCanVerify"]) {
    const line = component.split("\n").find((l) => l.includes("const " + name + " ="));
    assert.ok(line, name + " exists");
    assert.doesNotMatch(line, /resend/i, name + " must not depend on the resend countdown");
  }
  assert.match(component, /const newCanVerify = currentView\.verified && canVerify\(/, "no new-email verification before the current email is verified");
});
