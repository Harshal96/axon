/* Axon base — single TS entry point (CSS is imported by the app entry).
   Copied from Arc's UI/base (see ../arc/UI/README.md): tokens-first
   design system foundations, no app knowledge beyond the data model.
   types.ts  domain data model (app + components speak only this)
   ui.ts     UI-only types & tone vocabulary
   utils.ts  tiny pure helpers
   hooks.ts  shared React hooks                                        */
export * from "./types";
export * from "./ui";
export * from "./utils";
export * from "./hooks";
export * from "./layout";
export * from "./catalog";
