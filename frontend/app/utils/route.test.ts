import { describe, expect, it } from "vitest";
import { blankDraft, draftIsComplete, draftToRoute, parseUpstream, routeToDraft } from "~/utils/route";

describe("draftIsComplete", () => {
  it("waits for the fields the route type needs, and never for a name", () => {
    const draft = { ...blankDraft(), hosts: ["app.example.com"] };
    expect(draftIsComplete(draft)).toBe(false);
    expect(draftIsComplete({ ...draft, upstreams: ["192.168.1.10:80"] })).toBe(true);
  });
  it("ignores fields of other route types", () => {
    expect(draftIsComplete({ ...blankDraft(), type: "tcp", listen_port: 2222, upstreams: ["192.168.1.10:22"] })).toBe(true);
    expect(draftIsComplete({ ...blankDraft(), type: "redirect", hosts: ["old.example.com"] })).toBe(false);
  });
});

describe("draftToRoute", () => {
  const draft = { ...blankDraft(), hosts: ["app.example.com"], path_prefix: "/api", upstreams: ["192.168.1.10:80"], block_countries: ["CN"], geoip_enabled: true };

  it("sends no name for a new route, so the server names it", () => {
    const route = draftToRoute(draft);
    expect(route).not.toHaveProperty("name");
    expect(route).toMatchObject({ type: "http", geoip: { block_countries: ["CN"] }, dns: { target: "haproxy" } });
  });
  it("keeps the edited route's name and the fields the editor lacks", () => {
    const original = { ...draftToRoute(draft), name: "app-api", health_check: { path: "/up", interval: "30s" } } as Route;
    const route = draftToRoute({ ...routeToDraft(original), health_path: "/health" }, original);
    expect(route).toMatchObject({ name: "app-api", health_check: { path: "/health", interval: "30s" } });
  });
  it("saves the country switch with a list, and leaves the file's flag alone without one", () => {
    expect(draftToRoute({ ...draft, geoip_enabled: false })).toMatchObject({ geoip: { enabled: false, block_countries: ["CN"] } });
    const original = { ...draftToRoute(draft), name: "app-api", geoip: { enabled: true, block_countries: [] } } as Route;
    expect(draftToRoute({ ...routeToDraft(original) }, original).geoip).toEqual({ enabled: true, block_countries: [] });
  });
  it("turns a zone-default proxied flag into off for passthrough, which has no default", () => {
    expect(draftToRoute({ ...draft, type: "passthrough", cloudflare_proxied: null })).toMatchObject({ cloudflare: { proxied: false } });
  });
});

describe("parseUpstream", () => {
  it.each([
    ["http://192.168.1.2:81/", { address: "192.168.1.2:81", tls: false }],
    ["HTTPS://nas.lan/admin", { address: "nas.lan:443", tls: true }],
    ["http://192.168.1.2", { address: "192.168.1.2:80", tls: false }],
    [" 192.168.1.2:81 ", { address: "192.168.1.2:81" }],
  ])("reads %s", (value, expected) => expect(parseUpstream(value)).toEqual(expected));
});
