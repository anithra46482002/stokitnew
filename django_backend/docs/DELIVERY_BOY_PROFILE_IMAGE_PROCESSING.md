# Delivery Boy profile image processing

- Accepted upload extensions and decoded formats: JPG/JPEG, PNG, WEBP. The backend validates both the extension and the actual decoded format.
- The upload payload limit remains 5 MiB.
- Every accepted image is decoded, EXIF orientation is applied, converted to RGB/RGBA as appropriate, and re-encoded as WebP before PostgreSQL persistence. The stored MIME type is always `image/webp`.
- WebP quality is configured with `DELIVERY_BOY_PROFILE_IMAGE_WEBP_QUALITY` (integer 1–100; default 83). The `.env.example` documents the setting.
- Pixel width and height are not resized or capped by application code. The output preserves the orientation-corrected source dimensions and aspect ratio.
- Decoder safety: Pillow's built-in `Image.MAX_IMAGE_PIXELS` protection remains active; Pillow's decompression-bomb warning is promoted to a rejection. This is a decoder safety guard, not an image resize policy. On the installed Pillow default, the warning threshold is approximately 89.5 megapixels; the hard error threshold is approximately twice that.
- The output is generated as a fresh image without passing EXIF/XMP metadata to the WebP encoder, so camera and GPS metadata are not persisted.
- The protected Delivery Boy image endpoint serves the stored bytes with `Content-Type: image/webp`. Listings expose only the image-present flag/version, never the BYTEA data.
