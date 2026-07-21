# Codex Time Machine demo

This is a static Vite/React demonstration of the accepted sanitized evidence
bundle. It needs no API key, backend server, or live model.

## Local development

```text
npm install
npm run dev
npm run validate
npm run build
npm run preview
```

The demo works offline after dependencies are installed. It intentionally uses
system fonts and a bundled JSON file rather than remote fonts or APIs.

## Bundle provenance and replacement

The default input is `public/demo/R-SMOKE-WSL-001/demo_bundle.json`, copied from
the accepted sanitized `demo-data/` bundle. Replace it only with another
sanitized accepted bundle that has the same required top-level artifacts. The UI
rejects missing required evidence rather than fabricating values.

## Limitations

The demo renders observable engineering evidence, not hidden chain-of-thought.
It does not run Codex, score technical correctness, claim causality, or expose
private raw run evidence.
