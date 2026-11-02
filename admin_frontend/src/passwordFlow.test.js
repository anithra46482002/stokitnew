import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { cleanOtpInput, validateOtp, validateCurrentPassword, validateNewPasswordPair, validateRegisteredEmail, maskEmailForDisplay, formatCountdown, accountStatusInfo, describeFlowError } from "./passwordFlow.js";

const source = (name) => readFileSync(new URL(name, import.meta.url), "utf8");
test("OTP input is digits only and capped at six", () => { assert.equal(cleanOtpInput("12a345678"), "123456"); assert.equal(validateOtp("12345"), "The OTP must be exactly 6 digits."); assert.equal(validateOtp("123456"), ""); });
test("password validators report required and mismatch errors", () => { assert.equal(validateCurrentPassword(""), "Enter your current password."); assert.equal(validateNewPasswordPair("abcdefghij", "different").confirm_password, "The two passwords do not match."); assert.equal(validateNewPasswordPair("1234567890", "1234567890").new_password, "The password cannot be entirely numeric."); });
test("email masking and resend countdown are safe and predictable", () => { assert.equal(maskEmailForDisplay("admin@example.com"), "a***@example.com"); assert.equal(formatCountdown(65), "1:05"); assert.equal(formatCountdown(-2), "0:00"); assert.equal(validateRegisteredEmail("bad"), "Enter a valid email address, such as name@gmail.com."); });
test("profile status is read-only presentation and defaults unknown values to inactive", () => { assert.deepEqual(accountStatusInfo("ACTIVE"), { active: true, label: "Active" }); assert.equal(accountStatusInfo("disabled").label, "Inactive"); });
test("flow error helper surfaces server field errors and retry-after without echoing submitted data", () => { const result = describeFlowError({ response: { status: 429, data: { message: "Wait", retry_after: 30, errors: { otp: ["Invalid code"] } } } }); assert.equal(result.message, "Wait"); assert.equal(result.retryAfter, 30); assert.equal(result.fieldErrors.otp, "Invalid code"); });
test("profile UI calls authenticated profile endpoint and offers retry/change password", () => { const page = source("./ProfilePage.jsx"); assert.match(page, /api\.get\("profile"\)/); assert.match(page, /Retry loading profile/); assert.match(page, /Change Password/); assert.match(page, /Last Login/); });
test("change password uses backend endpoint contract in sequence and replaces fresh session token", () => { const page = source("./ChangePasswordDialog.jsx"); const endpoints = ["password/change/verify-current", "password/change/send-otp", "password/change/verify-otp", "password/change/confirm"]; let previous = -1; for (const endpoint of endpoints) { const next = page.indexOf(endpoint); assert.ok(next > previous, endpoint + " order"); previous = next; } assert.match(page, /verification_token:token/); assert.match(page, /replaceAdminToken\(r\.data\.token\)/); assert.doesNotMatch(page, /localStorage|sessionStorage/); });
test("forgot password uses existing generic send, verify, and reset contracts", () => { const page = source("./ForgotPasswordPage.jsx"); for (const endpoint of ["password/forgot/send-otp", "password/forgot/verify-otp", "password/forgot/reset"]) assert.ok(page.includes(endpoint)); assert.match(page, /reset_token:resetToken/); assert.match(page, /If an eligible account exists/); assert.match(page, /clearSensitive\(\)/); assert.doesNotMatch(page, /localStorage|sessionStorage/); });
test("login/profile/forgot routes are integrated and protected routes use existing guard", () => { const app = source("./App.jsx"); assert.match(app, /to="\/forgot-password"/); assert.match(app, /path="\/forgot-password"/); assert.match(app, /path="\/profile"/); assert.match(app, /topbar-profile-link/); });

test("profile uploads and persists Super Admin images, validates files, and preserves settings/navigation", () => {
 const page = source("./ProfilePage.jsx");
 assert.match(page, /ALLOWED_PROFILE_IMAGE_TYPES/);
 assert.match(page, /MAX_PROFILE_IMAGE_BYTES = 5 \* 1024 \* 1024/);
 assert.match(page, /URL\.createObjectURL/);
 assert.match(page, /api\.post\("profile\/image"/);
 assert.match(page, /profile_image_url/);
 assert.match(page, /compressed to WebP before being saved/);
 assert.doesNotMatch(page, /permanent saving is not currently supported/);
 assert.match(page, /Account Settings/);
 assert.match(page, /Account Status/);
 assert.match(page, /to="\/forgot-password"/);
 assert.match(page, /function goBack\(\)/);
 assert.match(page, /navigate\("\/"\)/);
 assert.match(page, /Change Password/);
});

test("authenticated users may open forgot-password from profile while anonymous users may access login/reset", () => {
 const session = source("./authSession.js");
 assert.match(session, /const onForgotPassword = pathname === '\/forgot-password'/);
 assert.match(session, /if \(!session && !onLogin && !onForgotPassword\) return '\/login'/);
 assert.match(session, /if \(session && onLogin\) return '\/'/);
});
test("Super Admin profile upload posts multipart data, verifies the saved profile, and handles loading/errors", () => {
 const page = source("./ProfilePage.jsx");
 assert.ok(page.includes('form.append("file", webpFile)'));
 assert.match(page, /createImageBitmap\(file\)/);
 assert.match(page, /image\/webp/);
 assert.match(page, /api\.post\("profile\/image", form\)/);
 assert.doesNotMatch(page, /api\.post\("profile\/image", form, \{ headers:/);
 assert.match(page, /const refreshed = await api\.get\("profile"\)/);
 assert.match(page, /savedProfile\?\.profile_image_url/);
 assert.match(page, /Profile image saved and verified successfully/);
 assert.match(page, /setImageSaving\(true\)/);
 assert.match(page, /disabled=\{imageSaving\}/);
 assert.match(page, /describeFlowError\(err, "Could not save the profile image\."\)/);
 assert.match(page, /profile\.profile_image_url/);
 const api = source("./api.js");
 assert.match(api, /config\.data instanceof FormData/);
 assert.match(api, /config\.headers\.delete\('Content-Type'\)/);
});
test("Super Admin identity settings use separate username and two-address email OTP workflows", () => {
 const component = source("./AccountIdentitySettings.jsx");
 for (const endpoint of ["profile/username/send-otp", "profile/username/verify-otp", "profile/email/send-current-otp", "profile/email/send-new-otp", "profile/email/verify-otp", "profile/email/complete"]) assert.ok(component.includes(endpoint), endpoint);
 assert.match(component, /currentView\.verified/);
 assert.match(component, /newView\.verified/);
 assert.match(component, /Verify and change username/);
 assert.match(component, /Save new email address/);
 assert.match(component, /cleanOtpInput/);
 assert.match(component, /describeFlowError/);
 const profile = source("./ProfilePage.jsx");
 assert.match(profile, /AccountIdentitySettings profile=\{profile\} onUpdated=\{load\}/);
});
test("account identity change UI updates the existing session profile and refreshes server profile", () => {
 const component = source("./AccountIdentitySettings.jsx");
 assert.match(component, /updateAdminSessionProfile\(\{ username: result\.data\.username \}\)/);
 assert.match(component, /updateAdminSessionProfile\(\{ email: result\.data\.email \}\)/);
 assert.match(component, /if \(onUpdated\) await onUpdated\(\)/);
 const session = source("./authSession.js");
 assert.match(session, /export function updateAdminSessionProfile/);
});
