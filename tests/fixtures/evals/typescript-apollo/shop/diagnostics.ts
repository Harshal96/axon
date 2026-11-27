import type { DocumentNode } from "graphql";
import { gql } from "@apollo/client";
import { client, wrongMount } from "./client";
import STORE_OPEN from "./status.graphql";

export function pollTwice() {
  return [client.query({ query: STORE_OPEN }), client.query({ query: STORE_OPEN })];
}

export function fromCache() {
  return client.query({ query: STORE_OPEN, fetchPolicy: "cache-only" });
}

export function computed(document: DocumentNode) {
  return client.query({ query: document });
}

export function wrongPath() {
  return wrongMount.query({ query: STORE_OPEN });
}

export function shadowed(client: { query(options: unknown): unknown }) {
  return client.query({ query: STORE_OPEN });
}

export function clientLocal() {
  return client.query({ query: gql`{ badge @client __typename }` });
}
