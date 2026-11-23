export async function start(): Promise<Response> {
  return await submit("test");
}

async function submit(body: string): Promise<Response> {
  return globalThis.fetch("https://ledger.example/entries", {
    method: "POST",
    body,
  });
}
