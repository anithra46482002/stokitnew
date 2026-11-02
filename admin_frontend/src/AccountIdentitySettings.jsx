import React from "react";
import api from "./api";
import { describeFlowError, cleanOtpInput } from "./passwordFlow";
import { updateAdminSessionProfile } from "./authSession";
import { useDeadlineCountdown } from "./useDeadlineCountdown";
import {
  EMPTY_STAGE, canResend, canVerify, createSubmitGuard, deadlineFromRetryAfter, deadlinesFromIssue, deriveStageView,
  formatOtpClock, otpEntryHint, resendLabel, verifiedDeadline,
} from "./otpTimer";

// Expiry and cooldown values always come from the API response. The countdowns below are display only and the
// backend stays authoritative for expiry, resend cooldowns, rate limits and single use.
function useStage(stage) {
  const expiresIn = useDeadlineCountdown(stage.expiresAt);
  const resendIn = useDeadlineCountdown(stage.resendAt);
  const verifiedIn = useDeadlineCountdown(stage.verifiedUntil);
  // lapsed: the stage was verified but its save window has since run out, so the screen must stop showing it as verified.
  return { ...stage, expiresIn, resendIn, verifiedIn, ...deriveStageView(stage, expiresIn, verifiedIn) };
}

function storedStage(view) {
  return { issued: view.issued, expiresAt: view.expiresAt, resendAt: view.resendAt, verified: view.verified, verifiedUntil: view.verifiedUntil };
}

function OtpTimerNote({ stage }) {
  if (stage.verified) {
    const left = stage.verifiedUntil ? " - valid for " + formatOtpClock(stage.verifiedIn) : "";
    return <p className="sk-otp-timer sk-otp-verified" role="status">{"Verified" + left}</p>;
  }
  if (!stage.issued) return null;
  if (stage.lapsed) return <p className="sk-otp-timer sk-otp-expired" role="status">Verification expired. Request a new code and verify again.</p>;
  if (stage.expired) return <p className="sk-otp-timer sk-otp-expired" role="status">This code has expired. Request a new code to continue.</p>;
  return <p className="sk-otp-timer" role="timer" aria-live="off">Code expires in <strong>{formatOtpClock(stage.expiresIn)}</strong></p>;
}

function OtpEntryHint({ otp, stage }) {
  const hint = otpEntryHint({ otp, issued: stage.issued, verified: stage.verified, expired: stage.expired });
  return hint ? <p className="sk-otp-timer" role="status">{hint}</p> : null;
}

export default function AccountIdentitySettings({ profile, onUpdated }) {
  const [username, setUsername] = React.useState(profile.username || "");
  const [usernameOtp, setUsernameOtp] = React.useState("");
  const [usernameStage, setUsernameStage] = React.useState(EMPTY_STAGE);
  const [email, setEmail] = React.useState("");
  const [currentOtp, setCurrentOtp] = React.useState("");
  const [newOtp, setNewOtp] = React.useState("");
  const [currentStage, setCurrentStage] = React.useState(EMPTY_STAGE);
  const [newStage, setNewStage] = React.useState(EMPTY_STAGE);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const [message, setMessage] = React.useState("");
  const guard = React.useRef(createSubmitGuard()).current;
  const mounted = React.useRef(true);
  React.useEffect(() => () => { mounted.current = false; }, []);

  const usernameView = useStage(usernameStage);
  const currentView = useStage(currentStage);
  const newView = useStage(newStage);


  // One request at a time, so a double click cannot submit twice. Success is reported only from the response of the backend.
  const run = (task) => guard.run(async () => {
    setBusy(true); setError(""); setMessage("");
    try {
      const response = await task();
      if (!mounted.current) return undefined;
      if (response.data && response.data.message) setMessage(response.data.message);
      return { ok: true, data: response.data };
    } catch (e) {
      if (!mounted.current) return undefined;
      const failure = describeFlowError(e, "The account change could not be completed.");
      setError(failure.message);
      return { ok: false, failure };
    } finally {
      if (mounted.current) setBusy(false);
    }
  });

  const issueCode = async (kind) => {
    const requests = {
      username: ["profile/username/send-otp", { username }, setUsernameStage, setUsernameOtp],
      current: ["profile/email/send-current-otp", { email }, setCurrentStage, setCurrentOtp],
      new: ["profile/email/send-new-otp", { email }, setNewStage, setNewOtp],
    };
    const [endpoint, body, setStage, setOtp] = requests[kind];
    const result = await run(() => api.post(endpoint, body));
    if (!result) return;
    if (result.ok && result.data && result.data.success) {
      // Timers start only now, from what the backend returned. Any previous code is superseded.
      setStage({ ...EMPTY_STAGE, issued: true, ...deadlinesFromIssue(result.data) });
      setOtp("");
    } else if (!result.ok) {
      const resendAt = deadlineFromRetryAfter(result.failure.retryAfter);
      if (resendAt) setStage((prev) => ({ ...prev, resendAt }));
      if (kind === "new" && result.failure.code === "current_email_not_verified") { setCurrentStage(EMPTY_STAGE); setNewStage(EMPTY_STAGE); }
    }
  };

  const verifyUsername = async (e) => {
    if (e) e.preventDefault();
    if (!canVerify({ otp: usernameOtp, busy, issued: usernameView.issued, verified: false, expired: usernameView.expired })) return;
    const result = await run(() => api.post("profile/username/verify-otp", { username, otp: usernameOtp }));
    if (result && result.ok && result.data && result.data.success) {
      updateAdminSessionProfile({ username: result.data.username });
      setUsernameStage(EMPTY_STAGE); setUsernameOtp("");
      if (onUpdated) await onUpdated();
    }
  };

  const verifyEmail = async (kind) => {
    const view = kind === "current" ? currentView : newView;
    const otp = kind === "current" ? currentOtp : newOtp;
    if (!canVerify({ otp, busy, issued: view.issued, verified: view.verified, expired: view.expired })) return;
    const result = await run(() => api.post("profile/email/verify-otp", { email, otp, stage: kind }));
    if (result && result.ok && result.data && result.data.success) {
      const next = { ...storedStage(view), issued: true, verified: true, verifiedUntil: verifiedDeadline(result.data) };
      if (kind === "current") { setCurrentStage(next); setCurrentOtp(""); } else { setNewStage(next); setNewOtp(""); }
    }
  };

  const completeEmail = async (e) => {
    e.preventDefault();
    if (busy || !currentView.verified || !newView.verified) return;
    const result = await run(() => api.post("profile/email/complete", { email }));
    if (result && result.ok && result.data && result.data.success) {
      updateAdminSessionProfile({ email: result.data.email });
      setEmail(""); setCurrentOtp(""); setNewOtp(""); setCurrentStage(EMPTY_STAGE); setNewStage(EMPTY_STAGE);
      if (onUpdated) await onUpdated();
    } else if (result && !result.ok && ["verification_required", "verification_expired"].includes(result.failure.code)) {
      // The server reports a missing or expired verification: drop the local verified state but keep the typed email.
      setCurrentStage(EMPTY_STAGE); setNewStage(EMPTY_STAGE); setCurrentOtp(""); setNewOtp("");
    }
  };

  const changeUsername = (value) => {
    setUsername(value);
    if (usernameStage.issued) { setUsernameStage(EMPTY_STAGE); setUsernameOtp(""); } // a code is bound to the username it was issued for
  };
  const changeEmail = (value) => {
    setEmail(value);
    setCurrentStage(EMPTY_STAGE); setNewStage(EMPTY_STAGE); setCurrentOtp(""); setNewOtp(""); // verification is per exact address
  };

  const sendUsername = (e) => { e.preventDefault(); return issueCode("username"); };
  const usernameCanVerify = canVerify({ otp: usernameOtp, busy, issued: usernameView.issued, verified: false, expired: usernameView.expired });
  const currentCanVerify = canVerify({ otp: currentOtp, busy, issued: currentView.issued, verified: currentView.verified, expired: currentView.expired });
  const newCanVerify = currentView.verified && canVerify({ otp: newOtp, busy, issued: newView.issued, verified: newView.verified, expired: newView.expired });

  return <section className="sk-profile-section" aria-labelledby="identity-settings-title">
    <h3 id="identity-settings-title">Change Username or Email</h3>
    <p className="sk-profile-note">Verification codes expire after five minutes and can only be used for their intended change.</p>
    {error && <p className="sk-msg sk-msg-error" role="alert">{error}</p>}
    {message && <p className="sk-msg sk-msg-success" role="status">{message}</p>}
    <form className="sk-form sk-identity-form" onSubmit={usernameView.issued ? verifyUsername : sendUsername}>
      <h4>Change Username</h4>
      <label className="sk-field">New username<input value={username} onChange={(e) => changeUsername(e.target.value)} minLength={3} maxLength={50} required readOnly={busy} /></label>
      {!usernameView.issued ? <button className="primary-action" type="submit" disabled={busy}>Send code to current email</button> : <>
        <OtpTimerNote stage={usernameView} />
        <label className="sk-field">6-digit code<input className="sk-otp-input" inputMode="numeric" autoComplete="one-time-code" maxLength={6} value={usernameOtp} onChange={(e) => setUsernameOtp(cleanOtpInput(e.target.value))} required readOnly={busy} disabled={usernameView.expired} /></label>
        <OtpEntryHint otp={usernameOtp} stage={usernameView} />
        <div className="button-row">
          <button className="primary-action" type="submit" disabled={!usernameCanVerify}>Verify and change username</button>
          <button type="button" onClick={() => issueCode("username")} disabled={!canResend({ busy, resendRemaining: usernameView.resendIn })}>{resendLabel({ issued: true, resendRemaining: usernameView.resendIn, firstLabel: "" })}</button>
        </div>
      </>}
    </form>
    <form className="sk-form sk-identity-form" onSubmit={completeEmail}>
      <h4>Change Email Address</h4>
      <label className="sk-field">New email address<input type="email" value={email} onChange={(e) => changeEmail(e.target.value)} maxLength={100} required readOnly={busy} /></label>
      <div className="sk-otp-stage">
        <h5>Step 1 - Verify your current email</h5>
        <button type="button" onClick={() => issueCode("current")} disabled={!email.trim() || currentView.verified || !canResend({ busy, resendRemaining: currentView.resendIn })}>{resendLabel({ issued: currentView.issued, resendRemaining: currentView.resendIn, firstLabel: "Send code to current email" })}</button>
        <OtpTimerNote stage={currentView} />
        <label className="sk-field">Current email verification code<input className="sk-otp-input" inputMode="numeric" autoComplete="one-time-code" maxLength={6} value={currentOtp} onChange={(e) => setCurrentOtp(cleanOtpInput(e.target.value))} readOnly={busy} disabled={!currentView.issued || currentView.verified || currentView.expired} /></label>
        <OtpEntryHint otp={currentOtp} stage={currentView} />
        <button type="button" onClick={() => verifyEmail("current")} disabled={!currentCanVerify}>{currentView.verified ? "Current email verified" : "Verify current email"}</button>
      </div>
      <div className="sk-otp-stage">
        <h5>Step 2 - Verify the new email</h5>
        <button type="button" onClick={() => issueCode("new")} disabled={!currentView.verified || !email.trim() || newView.verified || !canResend({ busy, resendRemaining: newView.resendIn })}>{resendLabel({ issued: newView.issued, resendRemaining: newView.resendIn, firstLabel: "Send code to new email" })}</button>
        <OtpTimerNote stage={newView} />
        <label className="sk-field">New email verification code<input className="sk-otp-input" inputMode="numeric" autoComplete="one-time-code" maxLength={6} value={newOtp} onChange={(e) => setNewOtp(cleanOtpInput(e.target.value))} readOnly={busy} disabled={!currentView.verified || !newView.issued || newView.verified || newView.expired} /></label>
        <OtpEntryHint otp={newOtp} stage={newView} />
        <button type="button" onClick={() => verifyEmail("new")} disabled={!newCanVerify}>{newView.verified ? "New email verified" : "Verify new email"}</button>
      </div>
      <button className="primary-action" type="submit" disabled={busy || !currentView.verified || !newView.verified}>Save new email address</button>
    </form>
  </section>;
}
