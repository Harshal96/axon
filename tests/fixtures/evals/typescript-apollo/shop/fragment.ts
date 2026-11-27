import { gql } from "@apollo/client";

export const STORE_STATUS = gql`
  fragment StoreStatus on Query {
    status
  }
`;
