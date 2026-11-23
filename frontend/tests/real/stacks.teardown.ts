import { test as teardown } from "@playwright/test";
import { forgetStacks, savedStacks, stopStack } from "./stack";

teardown("remove the stacks", async () => {
  await Promise.all(savedStacks().map(stopStack));
  forgetStacks();
});
