import express from "express";
import { ApolloServer } from "@apollo/server";
import { expressMiddleware } from "@as-integrations/express5";
import typeDefs from "./orders.graphql";
import { loadOrder } from "./orderResolver";

function cancel(_: unknown, args: { id: string }) {
  return args.id.length > 0;
}

const resolvers = {
  Query: {
    order: loadOrder,
    status() { return "open"; },
  },
  Mutation: { cancel },
};

export const examplePayload = {
  Query: {
    order() { return { id: "example" }; },
    status() { return "sample"; },
  },
};

const app = express();
const server = new ApolloServer({ typeDefs, resolvers });
await server.start();
app.use("/graphql", express.json(), expressMiddleware(server));
