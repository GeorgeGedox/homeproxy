import type { components } from "~~/api/schema";

export type Job = components["schemas"]["Job"];

const POLL_MS = 1000;

/** Poll a job until it finishes or fails. A job the API no longer knows ends as undefined. */
export async function waitForJob(api: ApiClient, id: string): Promise<Job | undefined> {
  for (;;) {
    const { data } = await api.GET("/api/v1/jobs/{job_id}", { params: { path: { job_id: id } } });
    if (!data || data.state === "finished" || data.state === "failed") return data;
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
  }
}
