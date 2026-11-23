const express = require("express");
const app = express();

function clearEntries(req, res) {
  res.sendStatus(204);
}

app.delete("/ledger/entries", clearEntries);
