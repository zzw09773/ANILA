# Synthetic CHT card emulator

This directory is development-only. Each emulator start generates a new root,
intermediate, card certificate, and private key in memory. The repository does
not contain a personal certificate, fixed CMS signature, or serialized private
key.

```bash
docker compose -f cht/docker-compose.yaml up --build
curl --fail http://localhost:16888/cht_api/synthetic-ca.pem \
  --output /tmp/anila-cht-synthetic-ca.pem
```

Point a development CSP process at that public bundle with
`CARD_CA_BUNDLE_PATH=/tmp/anila-cht-synthetic-ca.pem`. If CSP runs in a container,
mount the file read-only and use its container path. The default synthetic PIN
is `654321` and can be changed with `CHT_SYNTHETIC_PIN`.

The CA changes whenever the emulator restarts, so fetch the bundle again after
each restart. Keep nonce binding enabled: the emulator signs the actual
`tbsPackage.tbs`, and `CARD_DEV_SKIP_NONCE_BINDING` is neither required nor
permitted in a formal deployment. Production continues to use the bundled or
explicitly configured CSPKI public trust anchors and a real HiPKI component.
