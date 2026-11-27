import { ApolloClient, HttpLink, InMemoryCache } from "@apollo/client";

export const client = new ApolloClient({
  cache: new InMemoryCache(),
  link: new HttpLink({ uri: "https://orders.example.test/graphql" }),
});

export const wrongMount = new ApolloClient({
  cache: new InMemoryCache(),
  link: new HttpLink({ uri: "https://orders.example.test/admin" }),
});
