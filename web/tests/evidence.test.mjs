import { test } from "node:test";
import assert from "node:assert/strict";
import { evidenceLabel } from "../base/utils.ts";

test("manual provenance shows the contributing file without the entry id", () => {
  assert.equal(evidenceLabel("manual:services/pay/axon.service.yaml#charge"), "manual (services/pay/axon.service.yaml)");
  assert.equal(evidenceLabel("manual:axon.yaml"), "manual (axon.yaml)");
  assert.equal(evidenceLabel("manual:endpoint", { source_file: "team.yaml" }), "manual (team.yaml)");
});

test("inferred rules and unknown evidence retain meaningful labels", () => {
  assert.equal(evidenceLabel("client:payments"), "client:payments");
  assert.equal(evidenceLabel(undefined, undefined, "CALLS"), "CALLS");
  assert.equal(evidenceLabel(), "unknown rule");
});
