// Pure helpers for OTP expiry / resend countdowns (no React, no storage, no network).
// The backend is authoritative for expiry, cooldowns and rate limits: everything here only DISPLAYS the values the API
// returned (expires_in, resend_after, retry_after, verified_expires_in) and never invents a delay of its own.

// Whole seconds left until an absolute deadline (ms since epoch). 0 when there is no deadline or it has passed.
export function remainingSeconds(deadline, now = Date.now()) {
  if (!deadline) return 0;
  return Math.max(0, Math.ceil((deadline - now) / 1000));
}

// "04:59" style clock (zero-padded) for the code expiry and resend countdowns.
export function formatOtpClock(seconds) {
  const total = Math.max(0, Math.ceil(Number(seconds) || 0));
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
}

// Deadlines from a SUCCESSFUL issue response. Nothing starts unless the backend confirmed the code was issued.
export function deadlinesFromIssue(data, now = Date.now()) {
  const expiresIn = Number(data?.expires_in);
  const resendAfter = Number(data?.resend_after);
  return {
    expiresAt: expiresIn > 0 ? now + expiresIn * 1000 : 0,
    resendAt: resendAfter > 0 ? now + resendAfter * 1000 : 0,
  };
}

// Resend deadline from a rate-limit/cooldown error (HTTP 429 retry_after). 0 when the error carries none.
export function deadlineFromRetryAfter(retryAfter, now = Date.now()) {
  const seconds = Number(retryAfter);
  return seconds > 0 ? now + seconds * 1000 : 0;
}

// Deadline for how long a verified stage stays usable for Save (verified_expires_in). 0 = no limit reported.
export function verifiedDeadline(data, now = Date.now()) {
  const seconds = Number(data?.verified_expires_in);
  return seconds > 0 ? now + seconds * 1000 : 0;
}

export const EMPTY_STAGE = Object.freeze({ issued: false, expiresAt: 0, resendAt: 0, verified: false, verifiedUntil: 0 });

// Entering/verifying a code is never blocked by the resend timer; only by a request in flight, a finished stage or an expired code.
export function canVerify({ otp, busy, issued, verified, expired }) {
  return Boolean(issued) && !busy && !verified && !expired && String(otp ?? '').length === 6;
}

// Inline guidance while a code is partly typed ("3 of 6 digits"). Purely informative: it never enables or disables anything.
export function otpEntryHint({ otp, issued, verified, expired }) {
  const length = String(otp ?? '').length;
  if (!issued || verified || expired || length === 0 || length >= 6) return '';
  return `Enter all 6 digits of the code (${length} of 6 entered).`;
}

// Resend is available only once the backend-provided cooldown has run out.
export function canResend({ busy, resendRemaining }) {
  return !busy && !(Number(resendRemaining) > 0);
}

export function resendLabel({ issued, resendRemaining, firstLabel }) {
  if (Number(resendRemaining) > 0) return `Resend code in ${formatOtpClock(resendRemaining)}`;
  return issued ? 'Resend code' : firstLabel;
}

// A verified stage counts only while its window has not run out (verifiedUntil 0 = the API reported no limit).
export function isStageVerified(stage, verifiedRemaining) {
  return Boolean(stage.verified) && (!stage.verifiedUntil || verifiedRemaining > 0);
}

// What the screen should show for one stage. `lapsed`: it was verified but its save window has since run out, so it must stop
// counting as verified. `expired`: a code was issued, is not verified and its five-minute entry window has ended.
export function deriveStageView(stage, expiresRemaining, verifiedRemaining) {
  const verified = isStageVerified(stage, verifiedRemaining);
  return {
    verified,
    lapsed: Boolean(stage.verified) && !verified,
    expired: Boolean(stage.issued) && !verified && !(Number(expiresRemaining) > 0),
  };
}

// Prevents duplicate submissions: a second run() while one is in flight is ignored, and the lock is always released.
export function createSubmitGuard() {
  let locked = false;
  return {
    get locked() { return locked; },
    async run(task) {
      if (locked) return undefined;
      locked = true;
      try { return await task(); } finally { locked = false; }
    },
  };
}

// One interval at a time. start() replaces any previous interval; stop() (unmount) clears it. A generation token makes a
// tick that was already scheduled for a replaced/stopped countdown a no-op, so stale callbacks can never update state.
export function createDeadlineTicker({ onTick, now = Date.now, setIntervalFn = setInterval, clearIntervalFn = clearInterval, intervalMs = 1000 }) {
  let timerId = null;
  let generation = 0;
  const clear = () => {
    if (timerId !== null) { clearIntervalFn(timerId); timerId = null; }
  };
  return {
    start(deadline) {
      clear();
      generation += 1;
      const mine = generation;
      const first = remainingSeconds(deadline, now());
      onTick(first);
      if (first <= 0) return;
      timerId = setIntervalFn(() => {
        if (mine !== generation) return;
        const left = remainingSeconds(deadline, now());
        onTick(left);
        if (left <= 0) clear();
      }, intervalMs);
    },
    stop() {
      generation += 1;
      clear();
    },
    get active() { return timerId !== null; },
  };
}
