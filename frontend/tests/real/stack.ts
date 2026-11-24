import { execFile, spawnSync } from "node:child_process";
import { readFileSync, rmSync, writeFileSync } from "node:fs";
import { request as httpRequest } from "node:http";
import { Resolver } from "node:dns/promises";
import { tmpdir } from "node:os";
import { promisify } from "node:util";
import { join } from "node:path";
import { connect as tlsConnect } from "node:tls";
import { request as playwrightRequest } from "@playwright/test";

/** One stack per worker of the real project; `playwright.config.ts` runs that many workers. */
export const STACKS = 4;

/** The upstream of the real flows, the same pinned image the dev stack uses. */
const WHOAMI = "traefik/whoami:v1.11.0@sha256:200689790a0a0ea48ca45992e0450bc26ccab5307375b41c84dfc4f2475937ab";
const TIMEOUT = 90_000;

const run = promisify(execFile);
// Asynchronous, so the setup starts the stacks side by side.
const docker = async (...args: string[]) => (await run("docker", args, { encoding: "utf8", timeout: 120_000 })).stdout.trim();
const quiet = async (...args: string[]) => {
  try {
    await docker(...args);
  } catch {
    // Nothing to remove.
  }
};

/** A started stack, as plain data, so the setup project can hand it to the workers in a file. */
export interface Stack {
  /** The dashboard, on the container's published HTTPS port. */
  dashboard: string;
  /** HAProxy's published HTTP and HTTPS ports, and the DNS server's UDP port, on loopback. */
  httpPort: number;
  httpsPort: number;
  dnsPort: number;
  /** The upstream's name on the stack's network. */
  upstream: string;
  /** The HomeProxy container and the stack's network. */
  app: string;
  network: string;
}

async function poll<T>(what: string, read: () => Promise<T | undefined>): Promise<T> {
  const deadline = Date.now() + TIMEOUT;
  while (Date.now() < deadline) {
    const value = await read();
    if (value !== undefined) return value;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`${what} within ${TIMEOUT / 1000}s`);
}

/** Remove the stack's containers and network; a stack that is gone already is no error. */
export async function stopStack({ app, upstream, network }: Pick<Stack, "app" | "upstream" | "network">): Promise<void> {
  await quiet("rm", "--force", "--volumes", app, upstream);
  await quiet("network", "rm", network);
}

/**
 * Start the built image in setup mode and one whoami upstream on a network of their own. Ports are
 * published on random loopback ports, so the run never clashes with the dev stack. The host reaches
 * the container from the network's gateway, which is inside the LAN range setup detects.
 */
export async function startStack(prefix: string): Promise<Stack> {
  const project = process.env.COMPOSE_PROJECT_NAME;
  if (!project) throw new Error("COMPOSE_PROJECT_NAME is not set. Run `mise run test-e2e`.");
  const [network, app, upstream] = [`${prefix}-net`, `${prefix}-homeproxy`, `${prefix}-whoami`];
  await stopStack({ app, upstream, network });
  await docker("network", "create", network);
  await docker("run", "--detach", "--name", upstream, "--network", network, WHOAMI);
  // The internet is off for the run: these jobs fail at once on a closed port.
  const offline = [
    ...["HOMEPROXY_CF_RANGES_URL", "HOMEPROXY_RELEASES_URL", "HOMEPROXY_GEOIP_URL", "HOMEPROXY_UPDATE_URL"].flatMap((name) => ["--env", `${name}=http://127.0.0.1:9`]),
    // lego would otherwise reach Let's Encrypt, and then a DNS provider whose retries outlast a test.
    ...["acme-v02.api.letsencrypt.org", "acme-staging-v02.api.letsencrypt.org"].flatMap((host) => ["--add-host", `${host}:127.0.0.1`]),
  ];
  await docker(
    "run", "--detach", "--name", app, "--network", network, "--publish", "127.0.0.1::8443", "--publish", "127.0.0.1::80", "--publish", "127.0.0.1::443", "--publish", "127.0.0.1::5335/udp",
    "--env", "HOMEPROXY_BOOTSTRAP_LISTEN=0.0.0.0:8443", ...offline, `homeproxy:${project}`,
  );
  const published = async (port: string) => (await docker("port", app, port)).split("\n").at(-1) ?? "";
  const dashboard = `https://${await published("8443")}`;
  // The page cannot load before the container answers, so readiness is polled on the API.
  const api = await playwrightRequest.newContext({ ignoreHTTPSErrors: true });
  await poll("the dashboard did not answer", async () => ((await api.get(`${dashboard}/api/v1/healthz`).catch(() => undefined))?.ok() ? true : undefined));
  await api.dispose();
  const port = async (inside: string) => Number((await published(inside)).split(":").at(-1));
  return { dashboard, httpPort: await port("80"), httpsPort: await port("443"), dnsPort: await port("5335/udp"), upstream, app, network };
}

/** IFA_F_TENTATIVE and IFA_F_DADFAILED in the flags column of /proc/net/if_inet6. */
const TENTATIVE = 0x40;
const DAD_FAILED = 0x08;

/**
 * Wait until no IPv6 address on the host is still in duplicate address detection. A new network's
 * bridge and veths get their link-local addresses a second or two after the stack answers. Chromium
 * then drops every open connection, loopback ones too, and a page loading at that moment fails with
 * net::ERR_NETWORK_CHANGED. A host without IPv6 has no such file and nothing to wait for.
 */
export async function hostAddressesSettled(): Promise<void> {
  const tentative = () => {
    try {
      return readFileSync("/proc/net/if_inet6", "utf8").split("\n").some((line) => {
        const flags = Number.parseInt(line.trim().split(/\s+/)[4] ?? "0", 16);
        return (flags & TENTATIVE) !== 0 && (flags & DAD_FAILED) === 0;
      });
    } catch {
      return false;
    }
  };
  await poll("host IPv6 addresses stayed tentative", async () => (tentative() ? undefined : true));
}

/** Where the setup project leaves the started stacks for the workers and the teardown. */
const STACKS_FILE = join(tmpdir(), `${process.env.COMPOSE_PROJECT_NAME}-e2e-stacks.json`);
export const saveStacks = (stacks: Stack[]) => writeFileSync(STACKS_FILE, JSON.stringify(stacks));
export const savedStacks = (): Stack[] => JSON.parse(readFileSync(STACKS_FILE, "utf8")) as Stack[];
export const forgetStacks = () => rmSync(STACKS_FILE, { force: true });

/** The container log, for a failed test. The API writes it to stderr. */
export function stackLog(stack: Stack): string {
  const result = spawnSync("docker", ["logs", stack.app], { encoding: "utf8" });
  return result.stdout + result.stderr;
}

/** The setup token is printed only to the container log, so setup cannot start without reading it there. */
export const setupToken = (stack: Stack): Promise<string> =>
  poll("no setup token in the log", async () => /enter this setup token:\s+(\S+)/.exec(stackLog(stack))?.[1]);

/** The IPv4 addresses the stack's DNS server answers for `name`, or [] when it has none: an outside effect the dashboard cannot show. */
export async function resolveOnLan(stack: Stack, name: string): Promise<string[]> {
  const resolver = new Resolver({ timeout: 2000, tries: 1 });
  resolver.setServers([`127.0.0.1:${stack.dnsPort}`]);
  return resolver.resolve4(name).catch(() => []);
}

/** The DNS names and fingerprint of the certificate HAProxy serves for `host` on its HTTPS port: an outside effect the dashboard cannot show. */
export function servedCertificate(stack: Stack, host: string): Promise<{ names: string; fingerprint: string }> {
  return new Promise((resolve, reject) => {
    // Only the names are read, so the self-signed certificate needs no trust.
    const socket = tlsConnect({ host: "127.0.0.1", port: stack.httpsPort, servername: host, rejectUnauthorized: false }, () => {
      const cert = socket.getPeerCertificate();
      resolve({ names: cert.subjectaltname ?? "", fingerprint: cert.fingerprint256 });
      socket.end();
    });
    socket.on("error", reject);
  });
}

/** One request to HAProxy's HTTP port with a Host header, as a browser on the LAN would send it: what HAProxy serves is an outside effect the dashboard cannot show. */
export function viaHaproxy(stack: Stack, url: string, auth?: string): Promise<{ status: number; headers: Record<string, unknown>; body: string }> {
  const { host, pathname, search } = new URL(url);
  const headers: Record<string, string> = { host, ...(auth ? { authorization: `Basic ${Buffer.from(auth).toString("base64")}` } : {}) };
  return new Promise((resolve, reject) => {
    const req = httpRequest({ host: "127.0.0.1", port: stack.httpPort, path: pathname + search, headers }, (res) => {
      let body = "";
      res.on("data", (chunk) => (body += chunk));
      res.on("end", () => resolve({ status: res.statusCode ?? 0, headers: res.headers, body }));
    });
    req.on("error", reject);
    req.end();
  });
}
