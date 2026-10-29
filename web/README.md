# Axon Web — service & API catalog

The dashboard is a copy of Arc's `UI/` (see `../../arc/UI/README.md`),
adapted to Axon's domain: same design system, same chrome, no
component packs.

```
web/
  base/         design system foundations — copied from Arc UI/base
    tokens.css      colour / spacing / type / radius / shadow / z tokens (+ reset)
    primitives.css  primitive classes: buttons, forms, cards, sidebar chrome
    types.ts        shared DOMAIN data model (mirrors /api/v2 shapes)
    ui.ts           Tone, Theme light/dark/system/colour + hue
    utils.ts hooks.ts
    layout.ts catalog.ts
                    pure per-API graph layout + catalog derivations (node --test)
  components/   copied from Arc UI/components (import from "@components")
    primitives/     Button, Chip, PillTabs, Segmented · Page, PageHeader,
                    Section, Card, Stat, EmptyState
    data/           Spark, Ring, AreaChart (hand-rolled SVG)
    nav/            Sidebar, TopBar, Palette (⌘K)
    domain/         EgoGraph (animated per-API call flow),
                    catalog pieces (ApiTable, HealthBadge, MethodChip)
  src/          the app (Arc's platform/ equivalent)
    App.tsx         router + theme/connection/catalogue state
    pages/          ServicesPage, ServicePage, ApisPage,
                    ApiPage, AskPage, ToolsPages, IndexCard
    lib/            live-client.ts (/api/v2 client), demo-client.ts +
                    demo.ts (fixture dataset, demo build only)
  tests/        node --test suites over base/*.ts pure modules
```

## Run

```bash
# terminal 1: backend (from the repo root)
uv run axon-server            # http://127.0.0.1:8080

# terminal 2: web
cd web && npm install && npm run dev   # http://127.0.0.1:5174
npm run typecheck && npm test && npm run build
```

## Main build vs demo build

The data source is chosen at build time via the `@client` alias in
`vite.config.ts`; there is no runtime mode switch.

- **Main build** (`npm run dev` / `npm run build`): `@client` resolves to
  `src/lib/live-client.ts`, the `/api/v2` client. If the API is down the
  UI says **API unreachable** and shows no data. This build contains no
  demo code, labels, or fixture data — CI greps the bundle to prove it.
- **Demo build** (`npm run dev:demo` / `npm run build:demo`, i.e.
  `vite --mode demo`): `@client` resolves to `src/lib/demo-client.ts`,
  which serves the fixture graph from `src/lib/demo.ts` with no backend.
  Output goes to `dist-demo/` (`npm run preview:demo` serves it). The UI
  labels itself **Demo data**; actions that need the server (index,
  impact, onboard) are disabled or fail with that explanation.

`__AXON_DEMO__` (a Vite `define`, declared in `src/globals.d.ts`) gates
the few demo-only labels so they are removed from the main build at
bundle time.

## Rules (inherited from Arc)

- Tokens first; classes before inline styles; primitives before
  bespoke markup. App/component code imports `@base` / `@components`.
- `src/lib/*.ts` and `base/*.ts` pure modules use relative imports
  only — `node --test` loads them directly.
- Pages and `App.tsx` import the data source as `@client`, never
  `demo.ts` directly. Only `demo-client.ts` (and `tests/`) may import
  `demo.ts`; that is what keeps demo data out of the main build.
- Every number in the UI is graph-derived (services, APIs, calls,
  deprecated/spec-only flags). Axon has no runtime telemetry; do not
  add latency/traffic numbers.
- Call direction is shown by arrowheads plus animated dashes and
  packets on call edges. Motion honours `prefers-reduced-motion`,
  and every graph has a Play/Pause control.
