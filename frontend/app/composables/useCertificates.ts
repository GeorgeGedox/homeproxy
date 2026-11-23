import type { components } from "~~/api/schema";
import type { Job } from "~/utils/jobs";

export type Certificate = components["schemas"]["CertificateInfo"];

const POLL_MS = 3000;

/** Certificates and their issuance jobs. Jobs are polled while the page is open. */
export function useCertificates() {
  const api = useApi();
  const certificates = ref<Certificate[]>([]);
  const jobs = ref<Job[]>([]);
  // Two errors, so a later successful load clears its own failure but not a refused renewal.
  const loadError = ref("");
  const renewError = ref("");
  let timer: ReturnType<typeof setInterval> | undefined;
  /** Finished jobs at the last poll, so a newly finished one reloads the certificates. */
  let finished: number | undefined;

  async function loadCertificates(): Promise<void> {
    const { data, error: failure } = await api.GET("/api/v1/certificates");
    if (data) certificates.value = data;
    loadError.value = data ? "" : apiErrorMessage(failure, "The certificates could not be loaded.");
  }

  async function loadJobs(): Promise<void> {
    const { data } = await api.GET("/api/v1/jobs");
    if (!data) return;
    jobs.value = data.filter((job) => job.kind.startsWith("certificate."));
    const before = finished;
    finished = jobs.value.filter((job) => job.state === "finished").length;
    if (before !== undefined && finished !== before) await loadCertificates();
  }

  async function renew(name: string): Promise<void> {
    const { response, error: failure } = await api.POST("/api/v1/certificates/{name}/renew", { params: { path: { name } } });
    renewError.value = response.ok ? "" : apiErrorMessage(failure, "The renewal was not started.");
    await loadJobs();
  }

  onMounted(() => {
    void loadCertificates();
    void loadJobs();
    timer = setInterval(() => void (document.hidden || loadJobs()), POLL_MS);
  });
  onBeforeUnmount(() => clearInterval(timer));

  return { certificates, jobs, error: computed(() => loadError.value || renewError.value), renew };
}
