/** Pure function-catalog presentation helpers. */
import type { Facet, FunctionOwner, FunctionRow } from "./types.ts";

export function groupByName(rows: FunctionRow[]): { name: string; rows: FunctionRow[] }[] {
  const groups = new Map<string, FunctionRow[]>();
  for (const row of rows) {
    const name = row.symbol.name;
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name)!.push(row);
  }
  return [...groups].map(([name, rows]) => ({ name, rows }));
}
export function ownerChipLabel(owner: FunctionOwner, multiRepo: boolean): string {
  const suffix = `/${owner.kind}/${owner.id}`;
  const repo = owner.key.endsWith(suffix) ? owner.key.slice(0, -suffix.length) : owner.key.split("/")[0];
  return `${multiRepo ? `${repo} · ` : ""}${owner.kind} · ${owner.id || "unknown"}`;
}
export function facetSummary(selected: string[], facets: Facet[]): string {
  return selected.map(key => facets.find(f => f.key === key)?.label || key).join(", ");
}
export function functionQueryParams(query: import("./types.ts").FunctionQuery): URLSearchParams {
  const p = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (Array.isArray(value)) for (const v of value) p.append(key, v);
    else if (value !== undefined && value !== "") p.set(key, String(value));
  }
  return p;
}

/** A definition link requires a server-recorded absolute checkout root. */
export function definitionUrl(sourcePath: string | undefined, file: string, line: number): string | undefined {
  if (!sourcePath?.startsWith("/") || !file || file.startsWith("/") || file.split("/").includes("..")) return undefined;
  const path = `${sourcePath.replace(/\/$/, "")}/${file}`;
  return `vscode://file${path.split("/").map(encodeURIComponent).join("/")}:${Math.max(1, line)}`;
}
