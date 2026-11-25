<script setup lang="ts">
const dns = useDns();
const addOpen = ref(false);
const form = reactive({ name: "", target: "" });
const formRef = useTemplateRef("formRef");
const { error: refused, show: showRefused, clear: clearRefused } = useFormError(formRef);

function openAdd(): void {
  Object.assign(form, { name: "", target: "" });
  clearRefused();
  addOpen.value = true;
}

async function add(): Promise<void> {
  const failure = await dns.add(form.name.trim(), form.target.trim());
  if (failure) return showRefused(failure, "The record was not staged.");
  addOpen.value = false;
}
</script>

<template>
  <UDashboardPanel id="dns">
    <template #header>
      <UDashboardNavbar title="DNS">
        <template #right>
          <UButton icon="lucide:plus" label="Add record" @click="openAdd" />
        </template>
      </UDashboardNavbar>
    </template>
    <template #body>
      <UAlert v-if="dns.error.value" color="error" variant="subtle" :description="dns.error.value" />
      <template v-if="dns.loaded.value">
        <DnsServer
          :state="dns.server.value" :listen="dns.listen.value" :forward-to="dns.forwardTo.value" :forward-staged="dns.forwardStaged.value" :zones="dns.zones.value"
          :lines="dns.lines.value" @copy="dns.copyLines"
        />
        <DnsNames :names="dns.names.value" @remove="dns.remove" />
      </template>
      <UModal v-model:open="addOpen" title="Add a record" description="A name on your network that is not a site, such as a NAS or a printer">
        <template #body>
          <UForm id="dns-record" ref="formRef" :state="form" :validate-on="[]" class="space-y-4" @submit="add">
            <UFormField label="Name" name="name" description="Inside one of your domains" required>
              <UInput v-model="form.name" placeholder="nas.example.com" class="w-full" />
            </UFormField>
            <UFormField label="Address" name="target" description="The device's IPv4 address on your network" required>
              <UInput v-model="form.target" placeholder="192.168.1.30" class="w-full" />
            </UFormField>
            <UAlert v-if="refused" color="error" variant="subtle" :description="refused" />
          </UForm>
        </template>
        <template #footer>
          <UButton type="submit" form="dns-record" label="Stage change" />
          <UButton color="neutral" variant="ghost" label="Cancel" @click="addOpen = false" />
        </template>
      </UModal>
    </template>
  </UDashboardPanel>
</template>
