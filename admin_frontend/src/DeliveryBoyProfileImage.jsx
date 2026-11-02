import React from "react";
import api from "./api";
const h = React.createElement;

function useProtectedImage(id, hasImage, version) {
  const requestKey = String(id || "") + "|" + String(version || "");
  const [state, setState] = React.useState({ key: "", url: "", loading: false, error: "" });
  React.useEffect(() => {
    let active = true;
    let objectUrl = "";
    if (!id || !hasImage) return undefined;
    api.get("delivery-boy-image", { params: { id: id }, responseType: "blob" }).then((response) => {
      objectUrl = URL.createObjectURL(response.data);
      if (active) setState({ key: requestKey, url: objectUrl, loading: false, error: "" });
      else URL.revokeObjectURL(objectUrl);
    }).catch((error) => {
      if (active) setState({ key: requestKey, url: "", loading: false, error: error.response && error.response.status === 404 ? "No saved image" : "Image unavailable" });
    });
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [id, hasImage, version, requestKey]);
  if (state.key === requestKey) return state;
  return { url: "", loading: Boolean(id && hasImage), error: "" };
}
export function DeliveryBoyImageThumbnail({ row }) {
  const image = useProtectedImage(row && row.id, Boolean(row && row.has_profile_image), row && row.profile_image_version);
  if (!row || !row.has_profile_image) return h("span", { "aria-label": "No profile image" }, "—");
  if (image.loading) return h("span", { role: "status" }, "Loading image…");
  if (!image.url) return h("span", { title: image.error || "Image unavailable" }, "Image unavailable");
  const label = "Profile image for " + (row.full_name || row.username || "Delivery Boy");
  return h("img", { className: "delivery-boy-image-thumb", src: image.url, alt: label });
}

export function DeliveryBoyProfileImageInput({ field, value, onFileChange, onRemoveChange }) {
  const saved = useProtectedImage(field.deliveryBoyId, field.hasProfileImage, field.profileImageVersion);
  const [previewUrl, setPreviewUrl] = React.useState("");
  React.useEffect(() => () => { if (previewUrl) URL.revokeObjectURL(previewUrl); }, [previewUrl]);
  const removing = Boolean(field.removeImage);
  const shownUrl = previewUrl || (removing || !field.hasProfileImage ? "" : saved.url);
  function choose(event) {
    const file = event.target.files && event.target.files[0] ? event.target.files[0] : null;
    event.target.value = "";
    if (!file) return;
    onRemoveChange(false);
    setPreviewUrl(URL.createObjectURL(file));
    onFileChange(file);
  }
  return h("span", { className: "delivery-boy-profile-image-field" },
    shownUrl ? h("img", { className: "delivery-boy-image-preview", src: shownUrl, alt: previewUrl ? "Selected profile image preview" : "Current saved profile image" }) : null,
    saved.loading && !previewUrl && !removing ? h("small", { role: "status" }, "Loading saved image…") : null,
    saved.error && !previewUrl && !removing && field.hasProfileImage ? h("small", { className: "field-help" }, saved.error) : null,
    h("input", { type: "file", accept: ".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp", "aria-label": "Profile Image", onChange: choose }),
    h("small", { className: "field-help" }, "Optional. JPG, JPEG, PNG or WEBP, maximum 5 MB. The image is stored in the database."),
    value ? h("button", { type: "button", className: "image-secondary-action", onClick: () => { setPreviewUrl(""); onFileChange(null); } }, "Clear selected replacement") : null,
    !value && field.hasProfileImage && !removing ? h("button", { type: "button", className: "image-danger-action", onClick: () => onRemoveChange(true) }, "Remove saved image") : null,
    removing ? h("span", { className: "image-removal-pending", role: "status" }, "Saved image will be removed when you save.") : null,
    removing ? h("button", { type: "button", className: "image-secondary-action", onClick: () => onRemoveChange(false) }, "Undo image removal") : null
  );
}
