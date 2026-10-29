// Eval fixture: TypeScript is a known v2 gap (parser input only).
// When a TypeScript parser lands, this case must be promoted to a
// supported case with real endpoint expectations instead of zero.

import express from "express";

const app = express();

app.get("/items/:id", (req, res) => {
  res.json({ id: req.params.id });
});
