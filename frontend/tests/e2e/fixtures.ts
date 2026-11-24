import { createServer, type Server } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";
import { test as base, expect, type Page } from "@playwright/test";

const ROOT = fileURLToPath(new URL("../../.output/public", import.meta.url));
const TYPES: Record<string, string> = {
  ".html": "text/html",
  ".js": "text/javascript",
  ".css": "text/css",
  ".json": "application/json",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
};

function serve(): Promise<Server> {
  const server = createServer((req, res) => {
    void (async () => {
      const path = normalize(new URL(req.url ?? "/", "http://x").pathname);
      let file = join(ROOT, path);
      if (!file.startsWith(ROOT)) file = join(ROOT, "200.html");
      let body: Buffer;
      try {
        body = await readFile(extname(file) ? file : join(ROOT, "200.html"));
      } catch {
        file = join(ROOT, "200.html");
        body = await readFile(file);
      }
      const type = TYPES[extname(file)] ?? "text/html";
      res.writeHead(200, { "content-type": type });
      res.end(body);
    })();
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server)));
}

type Worker = { appURL: string };

/** Playwright test with a static server for the built app. API calls are mocked per test. */
export const test = base.extend<object, Worker>({
  appURL: [
    // eslint-disable-next-line no-empty-pattern
    async ({}, use) => {
      const server = await serve();
      const addr = server.address();
      const port = typeof addr === "object" && addr ? addr.port : 0;
      await use(`http://127.0.0.1:${port}`);
      server.close();
    },
    { scope: "worker" },
  ],
  baseURL: async ({ appURL }, use) => {
    await use(appURL);
  },
});

export { expect };

/** An API answer: the status and the JSON body, if any. */
export type Reply = [status: number, body?: unknown];

/** Answer every API call with `reply`, from the method, the path after `/api/v1` and the JSON body. No answer is a 404. */
export async function routeApi<Body>(page: Page, reply: (method: string, path: string, body: Body) => Reply | undefined): Promise<void> {
  await page.route("**/api/v1/**", (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/v1", "");
    const [status, data] = reply(request.method(), path, request.postDataJSON() as Body) ?? [404, {}];
    return route.fulfill({ status, contentType: "application/json", body: data === undefined ? "" : JSON.stringify(data) });
  });
}
