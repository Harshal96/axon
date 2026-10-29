/* Axon components — single entry point, copied from Arc's UI/components.
   Import from "@components" — never deep-import a file from outside
   this folder; add it here if it's missing.

   Layers (each may only depend on the ones above it):
     primitives/  design-system atoms & layout (no domain knowledge)
     data/        charts (hand-rolled SVG, {data,color} API)
     nav/         app chrome: sidebar, top bar, command palette
     domain/      Axon-specific composites (service map, API graph)  */

export * from "./primitives/primitives";
export * from "./primitives/layout";

export * from "./data/charts";

export * from "./nav/sidebar";
export * from "./nav/topbar";
export * from "./nav/palette";

export * from "./domain/graph";
export * from "./domain/catalog";
export * from "./domain/inspector";
export * from "./domain/agent";
export * from "./domain/model-picker";
