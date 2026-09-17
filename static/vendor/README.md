# Vendored

## hls.light.min.js

hls.js 1.7.3, Apache-2.0, from the npm registry.

Chrome and Firefox cannot play an HLS playlist on their own, and a live preview
needs one: a growing fragmented MP4 plays but cannot be sought. The file is kept
here rather than fetched from a CDN so the UI keeps working offline.

- tarball: https://registry.npmjs.org/hls.js/-/hls.js-1.7.3.tgz
- npm integrity: sha512-MsPlx6yVW4Qv4C7mEVou4/gk/5cN2dVxOTnrNjPSmyH2f0Ln+/UkG10Ax1PE6OvrmTVPKgQYozTtBaPOuCzq7A==
- verified with: openssl dgst -sha512 -binary hls.js-<version>.tgz | base64 -w0
