# Axon Web — service & API catalog

The dashboard is a copy of Arc's `UI/` (see `../../arc/UI/README.md`),
adapted to Axon's domain: same design system, same chrome, no
component packs.

```
web/
  base/         design system foundations — copied from Arc UI/base
    tokens.css      colour / spacing / type / radius / shadow / z tokens (+ reset)
    primitives.css  primitive classes: buttons, forms, cards, sidebar chrome
    types.ts        shared DOMAIN data model (mirrors /api/v1 shapes)
    ui.ts           Tone, Theme light/dark/system/colour + hue
    utils.ts hooks.ts
    layout.ts catalog.ts
                    pure per-API graph layout + catalog derivations (node --test)
  components/   copied from Arc UI/components (import from "@components")
    primitives/     Button, Chip, PillTabs, Segmented · Page, PageHeader,
                    Section, Card, Stat, EmptyState, MultiSelect
    data/           Spark, Ring, AreaChart (hand-rolled SVG)
    nav/            Sidebar, TopBar, Palette (⌘K)
    domain/         EgoGraph (animated per-API call flow),
                    catalog pieces (ApiTable, MethodChip)
  src/          the app (Arc's platform/ equivalent)
    App.tsx         router + theme/connection/catalogue state
    pages/          ServicesPage, ServicePage, ApisPage,
                    ApiPage, AskPage, FunctionsPage, ToolsPages, IndexCard
    lib/            live-client.ts (/api/v1 client), demo-client.ts +
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
  `src/lib/live-client.ts`, the `/api/v1` client. If the API is down the
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

## Large catalogs and API graphs

API tables show 100 rows per page. The catalog shows up to 20 service pills,
with remaining services available in a selector. Graph layout classifies nodes
in one edge pass and wraps each logical column after 12 cards; handler closures
are ordered breadth first. Initial framing centers the selected API at a readable
zoom, so large graphs may need panning. Internal leaf helpers are hidden by
default above 40 symbols; the helper toggle reveals them while API connection paths stay visible.
Partial graph responses display a badge with server-reported closure counts.

Pan and wheel updates are coalesced per animation frame. Node and edge layers
are cached across viewport changes. Motion uses at most 36 edges, prioritizes API
calls and handlers, and pauses during dragging or above 250 nodes / 80 edges.
Arrowheads retain call direction with motion paused.

## Direct links and browser navigation

Repository scope is the encoded path segment `/repos/<repo-id>`. Selection uses
query parameters:

- `/repos/sample` opens that repository's API catalog.
- `/repos/sample?service=items` opens its service detail page.
- `/repos/sample?service=items&api=<encoded-endpoint-id>` opens an API graph.
  `api` is the full endpoint node ID returned by `/api/v1/apis`; navigation
  encodes it with `URLSearchParams` and includes its owning service.
- `/repos/sample?view=apis&service=items` opens a service-filtered API catalog.
- `view=services|ask|function|impact|onboard` opens another page; Ask and
  Function links can also contain `q`.

The repository switcher changes scope; All repositories returns to `/`.
Catalog requests include the repository filter, and cross-repository API
navigation follows the endpoint's ownership. URLs initialize the page before
catalog loading, and navigation uses browser history for Back/Forward. Refresh
and pasted links work when the host serves `index.html` for `/repos/*` paths
(the Vite development and preview servers provide this SPA fallback).
`base/routing.ts` owns URL parsing/serialization and has pure regression tests.

Framework-inferred call edges retain their evidence rule and use dashed base strokes,
including when motion is paused or reduced. Hover titles identify the inference rule.

## Functions → APIs

The Functions page opens with 50 ranked symbols per page. Usage is static reach
from indexed API handlers (CALLS depth ≤ 12), ordered by services, APIs, direct
callers and name. It provides faceted Used in, Defined in and Kind multi-selects,
handler hiding, same-name grouping and an unreached view with static-analysis
blind spots. Selecting a row opens API path and direct-caller evidence in the
existing inspector shell. API links open the corresponding API graph.

Function URLs retain `q`, repeated `used`, `defined` and `kind`, `sort`, and `fn`.
Display controls use `hide_handlers=false`, `unreached=true`, `group=true`, and
`offset`; the repository remains in `/repos/<repo-id>`. Refresh and Back/Forward
restore this state. `MultiSelect` is exported through `@components` and supports
search, checkbox-style selection, arrow keys, Space, Home/End and Escape.
Definition links require a recorded source checkout root and open in VS Code;
older snapshots still expose a copyable definition location.

The repository switcher keeps the Functions view open, retaining its query,
kind and display controls while clearing repository-specific usage/owner filters,
selection and page offset. Older snapshots offer a copyable definition location;
fresh indexes record the checkout root for the local editor link.

## Ask page

Ask renders one plain summary, then compact endpoint relationship rows. Source
and target buttons open their API graphs; the muted file/line opens the caller
function. More than eight rows expand with “Show all”. Older responses fall back
to left-aligned citation rows. Static-analysis blind spots have a disclosure,
initially open for answers without rows. Interpretation, engine and elapsed query
time share a quiet metadata disclosure with intent, query and evidence details.
Query time is UI request duration, not API runtime telemetry.

Each answer also offers one collapsed “Show usage graph” slot (only one open
at a time). Tier 1 renders instantly from the answer rows already on the page —
no fetch — mirrors the first-eight/“Show all” state, highlights rows for a
selected node, and aggregates to service→service with counts above 24 nodes
or 40 edges. Citation-only answers invent no edges; they offer per-API
drill-down only. “View full graph” on one endpoint lazily fetches its existing
`/graph` ego graph once (cached per answer, inline retry on failure) into the
same slot in embedded `EgoGraph` mode; the list stays intact on error.
`src/lib/ask-graph.ts` holds the pure row→graph derivation with Node tests.

The empty state contains three text prompts and inline Skills. Once a chat starts,
the extra footer and Skills disappear. Thinking shows actual elapsed request time,
without simulated progress steps. Voice input is shown only with browser speech
recognition support. Native buttons/disclosures preserve keyboard access and
focus indicators; streaming and automatic scrolling respect reduced motion.
`src/lib/ask-format.ts` contains pure formatting helpers with Node tests.

Manual correction evidence in graph hover titles and function paths displays
`manual (<file>)`, using the contributing configuration file retained on the edge.
