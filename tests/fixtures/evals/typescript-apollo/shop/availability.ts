import { gql } from "@apollo/client";
import { client } from "./client";

const AVAILABILITY = gql`
  query Availability($show: Boolean!) {
    status @include(if: $show)
    version @skip(if: true)
  }
`;

export function readAvailability(show: boolean) {
  return client.query({ query: AVAILABILITY, variables: { show } });
}
