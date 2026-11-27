import { gql } from "@apollo/client";
import { STORE_STATUS } from "./fragment";

export const ORDER_PAGE = gql`
  query OrderPage($id: ID!) {
    current: order(id: $id) { id }
    ... on Query { repeated: order(id: $id) { id } }
    ...StoreStatus
  }
  ${STORE_STATUS}
`;
