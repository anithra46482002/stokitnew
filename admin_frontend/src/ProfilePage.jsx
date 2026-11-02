import React from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import api from "./api";
import { accountStatusInfo, describeFlowError } from "./passwordFlow";
import ChangePasswordDialog from "./ChangePasswordDialog";
import AccountIdentitySettings from "./AccountIdentitySettings";
import "./ProfilePage.css";

const MAX_PROFILE_IMAGE_BYTES = 5 * 1024 * 1024;
const ALLOWED_PROFILE_IMAGE_TYPES = new Set(["image/jpeg", "image/png", "image/webp", "image/gif"]);

export default function ProfilePage() {
  const [profile, setProfile] = React.useState(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState("");
  const [showChange, setShowChange] = React.useState(false);
  const [imagePreview, setImagePreview] = React.useState("");
  const [imageError, setImageError] = React.useState("");
  const [imageName, setImageName] = React.useState("");
  const [imageSaving, setImageSaving] = React.useState(false);
  const [imageSuccess, setImageSuccess] = React.useState("");
  const fileInput = React.useRef(null);
  const navigate = useNavigate();
  const location = useLocation();

  const load = React.useCallback(async () => {
    setLoading(true); setError("");
    try {
      const response = await api.get("profile");
      setProfile(response.data?.profile || null);
      if (!response.data?.profile) setError("The server did not return profile information.");
    } catch (err) {
      setProfile(null); setError(describeFlowError(err, "Could not load your profile.").message);
    } finally { setLoading(false); }
  }, []);

  React.useEffect(() => { load(); }, [load]);
  React.useEffect(() => () => { if (imagePreview.startsWith("blob:")) URL.revokeObjectURL(imagePreview); }, [imagePreview]);

  async function selectImage(event) {
    const file = event.target.files?.[0];
    setImageError("");
    setImageSuccess("");
    if (!file) return;
    if (!ALLOWED_PROFILE_IMAGE_TYPES.has(file.type)) {
      setImageError("Choose a JPG, PNG, WEBP, or GIF image.");
      event.target.value = "";
      return;
    }

    setImagePreview((previous) => { if (previous.startsWith("blob:")) URL.revokeObjectURL(previous); return URL.createObjectURL(file); });
    setImageName(file.name);
    setImageSaving(true);
    try {
      const bitmap = await createImageBitmap(file);
      try {
        let maxDimension = 1600;
        let quality = 0.84;
        let blob = null;
        for (let attempt = 0; attempt < 10; attempt += 1) {
          const scale = Math.min(1, maxDimension / Math.max(bitmap.width, bitmap.height));
          const canvas = document.createElement("canvas");
          canvas.width = Math.max(1, Math.round(bitmap.width * scale));
          canvas.height = Math.max(1, Math.round(bitmap.height * scale));
          const context = canvas.getContext("2d");
          if (!context) throw new Error("Could not prepare the image for compression.");
          context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
          blob = await new Promise((resolve, reject) => canvas.toBlob((result) => result ? resolve(result) : reject(new Error("WebP compression failed.")), "image/webp", quality));
          if (blob.type !== "image/webp") throw new Error("This browser does not support WebP image conversion.");
          if (blob.size <= MAX_PROFILE_IMAGE_BYTES) break;
          if (quality > 0.42) quality = Math.max(0.42, quality - 0.12);
          else maxDimension = Math.max(320, Math.floor(maxDimension * 0.75));
        }
        if (!blob || blob.size > MAX_PROFILE_IMAGE_BYTES) throw new Error("This image could not be compressed to the storage limit. Try a different image format.");
        const webpName = (file.name.replace(/\.[^.]+$/, "") || "profile-image") + ".webp";
        var webpFile = new File([blob], webpName, { type: "image/webp", lastModified: Date.now() });
      } finally {
        bitmap.close?.();
      }
      const form = new FormData();
      form.append("file", webpFile);
      const response = await api.post("profile/image", form);
      if (!response.data?.success || !response.data?.profile_image_url) {
        throw new Error(response.data?.message || "The server did not confirm that the profile image was saved.");
      }
      // Verify persistence by reading the profile back instead of trusting a preview or upload response alone.
      const refreshed = await api.get("profile");
      const savedProfile = refreshed.data?.profile;
      if (!savedProfile?.profile_image_url) {
        throw new Error("The upload returned successfully, but the saved profile image could not be verified. Refresh the profile or contact support.");
      }
      setProfile(savedProfile);
      window.dispatchEvent(new Event("stokit-profile-image-updated"));
      setImagePreview((previous) => { if (previous.startsWith("blob:")) URL.revokeObjectURL(previous); return ""; });
      setImageSuccess("Profile image saved and verified successfully.");
      setImageName(file.name);
    } catch (err) {
      setImageError(describeFlowError(err, "Could not save the profile image.").message);
    } finally {
      setImageSaving(false);
      if (event.target) event.target.value = "";
    }
  }

  function clearImagePreview() {
    setImagePreview((previous) => { if (previous.startsWith("blob:")) URL.revokeObjectURL(previous); return ""; });
    setImageName(""); setImageError(""); setImageSuccess("");
    if (fileInput.current) fileInput.current.value = "";
  }

  function goBack() {
    const from = location.state?.from;
    if (typeof from === "string" && from.startsWith("/") && !from.startsWith("//") && from !== "/login" && from !== "/forgot-password") {
      navigate(from);
      return;
    }
    navigate("/");
  }

  if (loading) return <section className="sk-profile" aria-busy="true"><div className="sk-profile-skeleton" role="status">Loading profile…</div></section>;
  if (error || !profile) return <section className="sk-profile"><div className="sk-profile-card sk-modal-body"><button type="button" onClick={goBack}>Back</button><h1>Admin Profile</h1><p className="sk-msg sk-msg-error" role="alert">{error || "Profile information is unavailable."}</p><button className="primary-action" type="button" onClick={load}>Retry loading profile</button></div></section>;

  const status = accountStatusInfo(profile.status);
  const fullName = profile.fullname || profile.full_name || profile.username || "—";
  const savedImage = profile.profile_image_url || profile.profile_picture_url || profile.profile_picture || profile.profile_image || "";
  const avatarLetter = String(profile.username || "A").slice(0, 1).toUpperCase();

  return <section className="sk-profile">
    <div className="page-heading sk-profile-heading">
      <div><h1>Admin Profile</h1><p>Your account information and security settings.</p></div>
      <div className="sk-profile-heading-actions"><button type="button" onClick={goBack}>Back</button><button type="button" onClick={load}>Refresh</button></div>
    </div>

    <article className="sk-profile-card">
      <header className="sk-profile-head">
        {imagePreview || savedImage ? <img className="sk-profile-avatar sk-profile-image" src={imagePreview || savedImage} alt="Profile" /> : <span className="sk-profile-avatar" aria-hidden="true">{avatarLetter}</span>}
        <div className="sk-profile-identity"><h2>{fullName}</h2><p>{profile.email || "No registered email"}</p><span className={`sk-status ${status.active ? "sk-status-active" : "sk-status-inactive"}`}>{status.label}</span></div>
      </header>

      {profile.role === "SUPER_ADMIN" && <section className="sk-profile-section" aria-labelledby="profile-image-title">
        <h3 id="profile-image-title">Profile Image</h3>
        <p className="sk-profile-note">Select a JPG, PNG, WEBP, or GIF image of any size. It will be automatically resized and compressed to WebP before being saved to your Super Admin profile.</p>
        <div className="sk-profile-image-actions">
          <input ref={fileInput} className="sk-profile-file-input" id="profile-image-file" type="file" accept="image/jpeg,image/png,image/webp,image/gif" onChange={selectImage} aria-describedby="profile-image-help" disabled={imageSaving} />
          <label className={`primary-action sk-profile-file-label${imageSaving ? " is-disabled" : ""}`} htmlFor="profile-image-file" aria-disabled={imageSaving}>{imageSaving ? "Saving Image..." : "Upload Image"}</label>
          {imagePreview && <button type="button" onClick={clearImagePreview} disabled={imageSaving}>Clear Preview</button>}
        </div>
        <p id="profile-image-help" className="sk-profile-note">{imageSaving ? `Uploading: ${imageName}` : imageName ? `Selected: ${imageName}` : "Images are stored with your profile after upload."}</p>
        {imageSuccess && <p className="sk-msg sk-msg-success" role="status">{imageSuccess}</p>}
        {imageError && <p className="sk-msg sk-msg-error" role="alert">{imageError}</p>}
      </section>}

      <section className="sk-profile-section" aria-labelledby="profile-settings-title">
        <h3 id="profile-settings-title">Account Settings</h3>
        <dl className="sk-profile-list">
          <div className="sk-profile-row"><dt>Full Name</dt><dd>{fullName}</dd></div>
          <div className="sk-profile-row"><dt>Username</dt><dd>{profile.username || "—"}</dd></div>
          <div className="sk-profile-row"><dt>Email</dt><dd>{profile.email || "—"}</dd></div>
          <div className="sk-profile-row"><dt>Role</dt><dd><span className="sk-role-chip">{profile.role || "Unassigned"}</span></dd></div>
          <div className="sk-profile-row"><dt>Account Status</dt><dd><span className={`sk-status ${status.active ? "sk-status-active" : "sk-status-inactive"}`}>{status.label}</span></dd></div>
          <div className="sk-profile-row"><dt>Last Login</dt><dd>{profile.last_login ? new Date(profile.last_login).toLocaleString() : "Not available"}</dd></div>
        </dl>
      </section>

      {profile.role === "SUPER_ADMIN" && <AccountIdentitySettings profile={profile} onUpdated={load} />}

      <section className="sk-profile-section" aria-labelledby="profile-security-title">
        <h3 id="profile-security-title">Security</h3>
        <p className="sk-profile-note">Keep your account secure by changing your password regularly.</p>
        <div className="sk-profile-actions sk-profile-security-actions">
          <button className="primary-action" type="button" onClick={() => setShowChange(true)}>Change Password</button>
          <Link className="sk-profile-link" to="/forgot-password">Forgot Password?</Link>
        </div>
      </section>
    </article>
    {showChange && <ChangePasswordDialog onClose={() => setShowChange(false)} />}
  </section>;
}
