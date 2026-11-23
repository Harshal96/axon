const express = require("express");
const transport = require("axios");
const app = express();
const cache = new Map();

async function status(req, res) {
  cache.get("/cached/status");
  const reply = await transport({ url: "https://status.example.test/live" });
  res.json(reply.data);
}

app.get("/status", status);
app.delete("/purge", async (req, res) => {
  await transport.delete("/ledger/entries");
  res.sendStatus(204);
});
