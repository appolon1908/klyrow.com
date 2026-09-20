# Media Asset Architecture

Klyrow accepts PNG, JPEG, and WebP uploads only. Filename extensions and browser-provided MIME types are advisory; the declared type must match the format detected by Pillow.

Upload bytes are consumed through the request stream. The application rejects an upload as soon as the streamed byte count exceeds the 10 MiB server limit or the asset's declared size. `Content-Length` is an early rejection hint, not the byte-count authority.

Pillow is the image-integrity authority. Validation opens immutable bytes, checks the detected format and dimensions, verifies the container, reopens the image, and fully decodes it. Dimensions are limited to 8192 per axis and the bounded pixel count is enforced before an asset can become `READY`.

The validation boundary is intentionally separate from malware inspection. Successful Pillow decoding proves image/container integrity only; it does not prove that content is malware-free. A future inspection provider may be placed between `VALIDATING` and `READY`.

Production object storage is not enabled by this implementation. The provider-neutral storage boundary and fail-closed production adapter remain separate work.
