import { getOrder } from "./inventory";

export async function loadOrder(_: unknown, args: { id: string }) {
  const response = await getOrder(args.id);
  return response.data;
}
