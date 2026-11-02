const OMIT = new Set(["has_profile_image", "profile_image_version", "created_at", "updated_at", "delivery_boy_id", "remove_profile_image"]);

export function buildDeliveryBoyFormData(values) {
  const form = new FormData();
  Object.entries(values || {}).forEach(([key, value]) => {
    if (OMIT.has(key) || value === undefined || value === null) return;
    if (key === "profile_image") {
      if (value && typeof value === "object" && typeof value.arrayBuffer === "function") {
        form.append("profile_image", value, value.name || "delivery-boy-profile");
      }
      return;
    }
    if (key === "joining_date" && values.id && String(value).trim() === "") return;
    form.append(key, typeof value === "boolean" ? String(value) : String(value));
  });
  if (values && values.remove_profile_image) form.append("remove_profile_image", "true");
  return form;
}
