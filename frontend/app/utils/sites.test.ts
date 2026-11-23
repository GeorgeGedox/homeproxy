import { describe, expect, it } from "vitest";
import { groupByZone, groupSites, onlyOn } from "~/utils/sites";

const http = (name: string, hosts: string[], path_prefix = "/") => ({ name, type: "http", hosts, path_prefix }) as unknown as Route;
const other = (name: string, type: "redirect" | "passthrough", hosts: string[]) => ({ name, type, hosts }) as unknown as Route;
const tcp = (name: string, listen_port: number) => ({ name, type: "tcp", listen_port }) as unknown as Route;
const shape = (routes: Route[]) => groupSites(routes).map((site) => [site.hosts.join(","), site.routes.map((r) => r.name)]);

describe("groupSites", () => {
  it("puts path routes of one host in one site, shortest path first", () => {
    expect(shape([http("api", ["example.com"], "/api"), http("root", ["example.com"])])).toEqual([["example.com", ["root", "api"]]]);
  });
  it("joins routes connected through a shared host, and says which hosts a narrower route serves", () => {
    const wide = http("wide", ["a.example.com", "b.example.com"]);
    const api = http("api", ["a.example.com"], "/api");
    const sites = groupSites([wide, api]);
    expect(sites).toHaveLength(1);
    const site = sites[0] as (typeof sites)[number];
    expect(site.hosts).toEqual(["a.example.com", "b.example.com"]);
    expect(onlyOn(api, site)).toEqual(["a.example.com"]);
    expect(onlyOn(wide, site)).toEqual([]);
  });
  it("merges two sites when a later route bridges them", () => {
    expect(shape([http("a", ["a.example.com"]), http("b", ["b.example.com"]), http("bridge", ["a.example.com", "b.example.com"], "/x")]))
      .toEqual([["a.example.com,b.example.com", ["a", "b", "bridge"]]]);
  });
  it("keeps redirect, passthrough and tcp routes as sites of their own, tcp last", () => {
    expect(shape([tcp("mc", 25565), other("www", "redirect", ["www.example.com"]), other("nas", "passthrough", ["nas.example.com"])]))
      .toEqual([["nas.example.com", ["nas"]], ["www.example.com", ["www"]], ["", ["mc"]]]);
  });
});

describe("groupByZone", () => {
  const site = (host?: string) => ({ key: host ?? "tcp", hosts: host ? [host] : [], routes: [] });
  const shape = (hosts: (string | undefined)[], zones: string[]) =>
    groupByZone(hosts.map(site), zones).map((group) => [group.name, group.sites.map((s) => s.key)]);

  it("puts sites under the longest zone that holds them, apex first, then Other and TCP", () => {
    expect(shape(["app.example.com", "example.com", "nas.home.example.com", "a.example.net", "stray.example.org", undefined], ["example.com", "home.example.com", "example.net"]))
      .toEqual([
        ["example.com", ["example.com", "app.example.com"]],
        ["example.net", ["a.example.net"]],
        ["home.example.com", ["nas.home.example.com"]],
        ["Other", ["stray.example.org"]],
        ["TCP", ["tcp"]],
      ]);
  });
  it("does not match a zone that is only a suffix of the host's label", () => {
    expect(shape(["myexample.com"], ["example.com"])).toEqual([["Other", ["myexample.com"]]]);
  });
});
