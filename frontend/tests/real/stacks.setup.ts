import { test as setup } from "@playwright/test";
import { hostAddressesSettled, saveStacks, STACKS, startStack } from "./stack";

// Every stack starts here, before any browser: a network that changes under a loading page fails it
// (hostAddressesSettled), so no stack starts or stops while the real tests run.
setup("start one stack per worker", async () => {
  setup.setTimeout(180_000);
  const prefix = `${process.env.COMPOSE_PROJECT_NAME}-e2e`;
  saveStacks(await Promise.all(Array.from({ length: STACKS }, (_, index) => startStack(`${prefix}-${index}`))));
  await hostAddressesSettled();
});
