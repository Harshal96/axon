/* Axon web — observability-style service & API catalog.
   The dashboard chrome is a copy of Arc's UI/platform App: Sidebar +
   TopBar + ⌘K Palette composed from ../base + ../components, crumb/body
   routing, stored theme + Colour hue, ⌘⇧L theme toggle. The main build
   reads /api/v2 via @client (live-client) and shows an honest
   "API unreachable" state when the server is down. Demo is a separate
   build (`--mode demo`) that swaps @client for the fixture dataset. */
import React, { useCallback, useEffect, useState } from "react";
import {
  DEFAULT_HUE, THEME_HUE_STORAGE_KEY, THEME_STORAGE_KEY, applyTheme, clampHue,
  useStoredState, type Theme,
} from "@base";
import { Palette, Sidebar, TopBar, palIcons, type PalGroup } from "@components";
import type { GNode, Route } from "@base/types";
import { nodeLabel } from "@base/catalog";
import * as api from "@client";
import { ServicesPage } from "./pages/ServicesPage";
import { ServicePage } from "./pages/ServicePage";
import { ApisPage } from "./pages/ApisPage";
import { ApiPage } from "./pages/ApiPage";
import { AskPage } from "./pages/AskPage";
import { FunctionPage, ImpactPage, OnboardPage } from "./pages/ToolsPages";

export function App(): React.ReactElement {
  const [connected, setConnected] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [palette, setPalette] = useState(false);
  const [theme, setTheme] = useStoredState<Theme>(THEME_STORAGE_KEY, "dark");
  const [hueRaw, setHueRaw] = useStoredState(THEME_HUE_STORAGE_KEY, String(DEFAULT_HUE));
  const hue = clampHue(Number(hueRaw) || DEFAULT_HUE);
  const setHue = (h: number) => setHueRaw(String(clampHue(h)));
  const [route, setRoute] = useState<Route>({ kind: "apis" });
  const [tick, setTick] = useState(0);
  const [services, setServices] = useState<GNode[]>([]);
  const [apis, setApis] = useState<GNode[]>([]);

  useEffect(() => applyTheme(theme, hue), [theme, hue]); // system = no data-theme attribute
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setPalette(p => !p); }
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === "l")
        setTheme(theme === "light" ? "dark" : "light");
    };
    addEventListener("keydown", h); return () => removeEventListener("keydown", h);
  }, [theme, setTheme]);

  useEffect(() => {
    let live = true;
    api.probe().then((ok) => { if (live) setConnected(ok); });
    return () => { live = false; };
  }, [tick]);
  useEffect(() => {
    let live = true;
    api.fetchServices().then((s) => { if (live) setServices(s); }).catch(() => undefined);
    api.fetchApis().then((a) => { if (live) setApis(a); }).catch(() => undefined);
    return () => { live = false; };
  }, [tick]);

  const bump = useCallback(() => setTick((t) => t + 1), []);
  const openApi = useCallback((id: string) => setRoute({ kind: "api", id }), []);
  const repos = [...new Set(services.map((s) => s.repo_id).filter(Boolean))];

  const paletteGroups = (q: string): PalGroup[] => {
    const has = (n: string) => !q || n.toLowerCase().includes(q);
    return [
      { name: "Suggestions", items: [
        { icon: palIcons.agent, name: "Ask Axon…", run: () => setRoute({ kind: "ask" }) },
        { icon: palIcons.plus, name: "Index a repo", run: () => setRoute({ kind: "services" }) },
        { icon: palIcons.project, name: "Onboard a repo", run: () => setRoute({ kind: "onboard" }) },
        { icon: theme === "light" ? palIcons.moon : palIcons.sun,
          name: theme === "light" ? "Switch to dark mode" : "Switch to light mode",
          kbd: "⌘ ⇧ L", run: () => setTheme(theme === "light" ? "dark" : "light") },
        { icon: palIcons.sun, name: "Colour theme — a hint of your colour",
          run: () => setTheme("colour") },
        { icon: palIcons.side, name: "Toggle sidebar", kbd: "[",
          run: () => setCollapsed(!collapsed) },
      ].filter(i => has(i.name)) },
      { name: "Services", items: services.filter(s => has(s.service_id)).map(s => ({
        icon: palIcons.project, name: s.service_id, sub: s.repo_id,
        right: `${s.api_count ?? 0} APIs`,
        run: () => setRoute({ kind: "service", id: s.service_id }) })) },
      { name: q ? "APIs" : "APIs in catalog", items: apis
        .filter(a => has(nodeLabel(a)) || has(a.service_id))
        .slice(0, q ? 10 : 5).map(a => ({
          icon: a.props.deprecated === "true" ? palIcons.done : palIcons.todo,
          name: nodeLabel(a), sub: a.service_id, right: a.props.operation_id,
          run: () => setRoute({ kind: "api", id: a.id }) })) },
      { name: "Navigation", items: [
        { icon: palIcons.agent, name: "Go to Ask Axon", kbd: "G A", run: () => setRoute({ kind: "ask" }) },
        { icon: palIcons.project, name: "Go to Services", kbd: "G S", run: () => setRoute({ kind: "services" }) },
        { icon: palIcons.project, name: "Go to API catalog", kbd: "G C", run: () => setRoute({ kind: "apis" }) },
        { icon: palIcons.search, name: "Go to Function → APIs", run: () => setRoute({ kind: "function" }) },
        { icon: palIcons.project, name: "Go to PR impact", run: () => setRoute({ kind: "impact" }) },
        { icon: palIcons.ws, name: "Go to Onboard repo", run: () => setRoute({ kind: "onboard" }) },
        ...(__AXON_DEMO__ ? [] : [{ icon: palIcons.settings, name: "Reconnect to API", run: bump }]),
      ].filter(i => has(i.name)) },
    ];
  };

  const ws = repos.length === 1 ? repos[0] : "Axon";
  let crumb: string[] = [ws, "API catalog"];
  let body: React.ReactNode = null;
  if (route.kind === "services") {
    crumb = [ws, "Services"];
    body = <ServicesPage services={services} apis={apis} go={setRoute} bump={bump} />;
  } else if (route.kind === "service") {
    crumb = [ws, "Services", route.id];
    body = <ServicePage id={route.id} services={services} apis={apis}
      go={setRoute} openApi={openApi} />;
  } else if (route.kind === "apis") {
    crumb = route.service ? [ws, "APIs", route.service] : [ws, "API catalog"];
    body = <ApisPage service={route.service ?? ""} services={services}
      openApi={openApi} goService={(id) => setRoute({ kind: "service", id })} />;
  } else if (route.kind === "api") {
    const ep = apis.find((a) => a.id === route.id);
    crumb = [ws, "APIs", ep ? nodeLabel(ep) : "API"];
    body = <ApiPage id={route.id} openApi={openApi} go={setRoute} />;
  } else if (route.kind === "ask") {
    crumb = [ws, "Ask Axon"];
    body = <AskPage key={route.q ?? "ask"} initial={route.q ?? ""}
      openApi={openApi} go={setRoute} />;
  } else if (route.kind === "function") {
    crumb = [ws, "Function → APIs"];
    body = <FunctionPage key={route.q ?? ""} initial={route.q ?? ""} openApi={openApi} />;
  } else if (route.kind === "impact") {
    crumb = [ws, "PR impact"];
    body = <ImpactPage />;
  } else if (route.kind === "onboard") {
    crumb = [ws, "Onboard repo"];
    body = <OnboardPage />;
  }

  return (
    <div style={{ display: "flex", height: "100%" }}>
      <Sidebar services={services} apis={apis} repos={repos}
        route={route} go={setRoute} connected={connected}
        collapsed={collapsed} theme={theme} setTheme={setTheme} hue={hue} setHue={setHue}
        onPalette={() => setPalette(true)} onReconnect={bump} />
      <main style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, background: "var(--bg-1)", borderRadius: "12px 0 0 0" }}>
        <TopBar crumb={crumb} collapsed={collapsed} setCollapsed={setCollapsed}
          onPalette={() => setPalette(true)} right={
            <span className={`chip ${connected && !__AXON_DEMO__ ? "tone-ok" : "tone-risk"}`}>
              {__AXON_DEMO__ ? "● demo data" : connected ? "● live" : "○ API unreachable"}</span>} />
        {body}
      </main>
      {palette && <Palette onClose={() => setPalette(false)} groups={q => paletteGroups(q)} />}
    </div>
  );
}
