import transport from "axios";

export const start = async () => {
  return transport.get("https://ledger.example/entries");
};
