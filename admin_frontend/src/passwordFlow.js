// Pure helpers for the Admin Profile / Change Password / Forgot Password UI (no React, no storage, no network).
// The server is always authoritative: these checks only give instant feedback and never replace backend validation.
import { validateEmail } from './fieldValidation.js';

export const OTP_LENGTH = 6;
export const MIN_PASSWORD_LENGTH = 10;

export function cleanOtpInput(value) {
  return String(value ?? '').replace(/\D/g, '').slice(0, OTP_LENGTH);
}

export function validateOtp(value) {
  const otp = String(value ?? '').trim();
  if (!otp) return 'Enter the 6-digit OTP from your email.';
  if (!/^\d{6}$/.test(otp)) return 'The OTP must be exactly 6 digits.';
  return '';
}

export function validateCurrentPassword(value) {
  return value ? '' : 'Enter your current password.';
}

export function validateRegisteredEmail(value) {
  return validateEmail(value);
}

// Returns { new_password?, confirm_password? }; an empty object means the pair looks acceptable.
export function validateNewPasswordPair(newPassword, confirmPassword) {
  const errors = {};
  if (!newPassword) errors.new_password = 'Enter a new password.';
  else if (newPassword.length < MIN_PASSWORD_LENGTH) errors.new_password = `Use at least ${MIN_PASSWORD_LENGTH} characters.`;
  else if (/^\d+$/.test(newPassword)) errors.new_password = 'The password cannot be entirely numeric.';
  if (!confirmPassword) errors.confirm_password = 'Confirm the new password.';
  else if (newPassword !== confirmPassword) errors.confirm_password = 'The two passwords do not match.';
  return errors;
}

export function maskEmailForDisplay(email) {
  const [local, domain] = String(email ?? '').split('@');
  if (!local || !domain) return '';
  return `${local[0]}***@${domain}`;
}

// "1:05" style label for the resend countdown.
export function formatCountdown(seconds) {
  const total = Math.max(0, Math.ceil(Number(seconds) || 0));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

export function accountStatusInfo(status) {
  const active = String(status ?? '').toUpperCase() === 'ACTIVE';
  return { active, label: active ? 'Active' : 'Inactive' };
}

// Turns an axios error into { message, fieldErrors, retryAfter, code } without ever echoing request data.
export function describeFlowError(error, fallback = 'Something went wrong. Please try again.') {
  const response = error?.response;
  if (!response) {
    return { message: 'Could not reach the server. Check your connection and try again.', fieldErrors: {}, retryAfter: 0, code: 'network' };
  }
  const data = response.data && typeof response.data === 'object' ? response.data : {};
  const fieldErrors = {};
  if (data.errors && typeof data.errors === 'object') {
    for (const [field, value] of Object.entries(data.errors)) {
      fieldErrors[field] = Array.isArray(value) ? value.join(' ') : String(value);
    }
  }
  const retryAfter = Number(data.retry_after) > 0 ? Number(data.retry_after) : 0;
  let message = typeof data.message === 'string' && data.message ? data.message : typeof data.detail === 'string' ? data.detail : '';
  if (!message) message = response.status === 429 ? 'Too many attempts. Please wait and try again.' : fallback;
  return { message, fieldErrors, retryAfter, code: typeof data.code === 'string' ? data.code : `http_${response.status}` };
}
