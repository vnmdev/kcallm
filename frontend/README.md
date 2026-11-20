# KcaLLM frontend

A React/TypeScript client for the Python API. The backend owns nutrition arithmetic, validation and confirmed records. The browser displays server previews and keeps unfinished input in IndexedDB.

## Run

From this directory:

```sh
npm ci
npm run dev
```

Vite proxies `/api` to `http://127.0.0.1:8000`. Start the backend separately using the repository's root instructions. `npm run build` type-checks and produces `dist/`, which the backend can serve from the same origin.

## Code map

- `src/App.tsx` loads shared data and coordinates navigation. Refresh request generations prevent late responses from replacing the selected day.
- `src/pages/Today.tsx` owns the capture, review and confirmation flow, along with diary entry editing. It never computes nutrients.
- `src/pages/Catalogue.tsx` reviews manual, label-extracted and imported food drafts before saving approved facts.
- `src/pages/Settings.tsx` manages targets, explicit model selection, ChatGPT connection instructions and backup/restore.
- `src/api.ts` handles both documented API error formats. `src/types.ts` contains wire types and display formatting.
- `src/draft.ts` serialises IndexedDB writes for capture and catalogue drafts. The exact batch request is persisted before confirmation is sent.

## Verify

```sh
npm run build
npx playwright install chromium --only-shell
npm run test:ui
```

Browser regressions use synthetic data and intercept every API request. They test lost-response retries across reload, clarification resolution, stale entry edits, out-of-order day responses and photo-draft persistence/cancellation. The photo test uses a small synthetic image and a phone-sized viewport. It does not establish actual Android camera capture or provider vision support.

The separate real-backend integration test must point at a disposable data directory with an empty active catalogue. It writes synthetic records, uses the UI to verify create/preview/confirm/edit/undo, saves desktop/mobile screenshots to ignored `artifacts/`, removes its entries, archives its test food and restores the prior targets. It does not call AI or authenticate an account:

```sh
KCALLM_INTEGRATION_URL=http://127.0.0.1:8000 \
KCALLM_INTEGRATION_ALLOW_WRITE=1 npm run test:ui -- integration.spec.ts
```

Without both environment settings this test is skipped. Run it only against an explicitly isolated backend, never a personal diary. The root backend instructions explain how to choose a separate data directory.

`npm run format` applies the project's frontend formatting. Test results, browser traces, dependencies and build output are ignored.

## Behaviour to preserve

An uncertain save keeps its original request ID and body. Retrying cannot become a new operation. After a confirmed response the app fetches the current diary instead of rendering a potentially old receipt. Ambiguous model rows need explicit resolution or removal. An edit cannot adopt a newer entry revision silently. Unknown nutrient values display as a dash, never as zero. Photos remain in browser drafts until confirmed or cancelled; backend connectivity is still required for saves.

Target remainders come from the backend's signed `Day.remaining` values. The client only formats them as remaining or over target; it does not subtract nutrient totals.
