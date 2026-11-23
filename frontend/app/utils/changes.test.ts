import { describe, expect, it } from "vitest";
import { snapshotText } from "./changes";

const snapshot = (message: string, changes: { kind: "route"; action: "added"; label: string }[] = []) => ({ id: "1", created: "", message, changes });

describe("snapshotText", () => {
  it.each([
    ["apply saved config", "Saved files applied again"],
    ["edit config.yaml; delete routes/nas.yaml", "Settings changed, Route nas removed"],
    ["add routes/a.yaml; add routes/b.yaml; add domains/example.com.yaml; add routes/c.yaml; add routes/d.yaml", "Route a added, Route b added, Domain example.com added and 2 more"],
    ["restore 20261121T140200Z", "restore 20261121T140200Z"],
  ])("reads the stored message %s", (message, text) => {
    expect(snapshotText(snapshot(message))).toBe(text);
  });

  it("prefers the summary of the apply", () => {
    expect(snapshotText(snapshot("add routes/app.yaml", [{ kind: "route", action: "added", label: "app.example.com" }]))).toBe("Route app.example.com added");
  });
});
