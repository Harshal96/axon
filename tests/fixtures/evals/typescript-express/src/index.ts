import express, { type Request, type Response } from "express";
import transport from "axios";
import { catalog as catalogApi } from "./clients";

const app = express();

function loadItem(id: string) {
  return catalogApi.get<{ id: string }>(`/v1/items/${id}`);
}

app.get("/v1/items/:id", async (req, res) => {
  const item = await loadItem(req.params.id);
  res.json(item.data);
});

async function publishEvent(req: Request, res: Response) {
  await transport.request({
    method: "post",
    url: "https://audit.example.test/events/ingest",
    data: req.body,
  });
  res.sendStatus(202);
}

app.post("/events", publishEvent);
