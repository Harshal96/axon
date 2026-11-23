import transport from "axios";

export const catalog = transport.create({ baseURL: process.env.CATALOG_URL });
