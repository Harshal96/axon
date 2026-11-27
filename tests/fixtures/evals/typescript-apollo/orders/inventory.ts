import axios from "axios";

export function getOrder(id: string) {
  return axios.get(`https://inventory.example.test/orders/${id}`);
}
