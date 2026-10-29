import { useState } from "react";
import { Card } from "@components";
import * as api from "@client";

/** "Index a repo on the server" card (main build only; indexing needs
    the live API, so the demo build drops this card at build time). */
export function IndexCard({ bump }: { bump: () => void }) {
  const [repoPath, setRepoPath] = useState("");
  const [msg, setMsg] = useState("");
  if (__AXON_DEMO__) return null;
  const doIndex = async (): Promise<void> => {
    setMsg("Indexing…");
    try {
      const r = await api.indexRepo(repoPath);
      setMsg(`Indexed ${r.repo}: ${r.nodes} nodes, ${r.edges} edges, `
        + `${r.unresolved.length} unresolved.`);
      bump();
    } catch (e) {
      setMsg(`Index failed: ${String(e)}`);
    }
  };
  return (
    <Card>
      <label className="lab" htmlFor="repo-path">
        Index a repo on the server (needs axon.yaml)
      </label>
      <div className="row">
        <input id="repo-path" className="field grow" placeholder="/path/to/repo"
          value={repoPath} onChange={(e) => setRepoPath(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && repoPath) void doIndex(); }} />
        <button className="btn primary" disabled={!repoPath} onClick={() => void doIndex()}>
          Index
        </button>
      </div>
      {msg && <p className="f-hint">{msg}</p>}
    </Card>
  );
}
