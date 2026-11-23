// Eval fixture for Express route registration through an anonymous callback.

import express from "express";

const app = express();

app.get("/items/:id", (req, res) => {
  res.json({ id: req.params.id });
});
