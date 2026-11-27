import express, { type Request, type Response } from "express";
import { gql } from "@apollo/client";
import { client } from "./client";
import { ORDER_PAGE } from "./orderDocument";
import STORE_OPEN from "./status.graphql";

const app = express();

async function orderPage(req: Request, res: Response) {
  const result = await client.query({ query: ORDER_PAGE, variables: { id: req.params.id } });
  res.json(result.data);
}

async function storeOpen(_req: Request, res: Response) {
  const result = await client.query({ query: STORE_OPEN });
  res.json(result.data);
}

async function cancelOrder(req: Request, res: Response) {
  const result = await client.mutate({
    mutation: gql`mutation Cancel($id: ID!) { cancel(id: $id) }`,
    variables: { id: req.params.id },
  });
  res.json(result.data);
}

app.get("/orders/:id", orderPage);
app.get("/status", storeOpen);
app.post("/orders/:id/cancel", cancelOrder);
