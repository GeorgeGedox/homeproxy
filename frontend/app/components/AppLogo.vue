<template>
  <span class="flex items-center gap-2 text-highlighted">
    <!-- The choke point: outside traffic narrows into the proxy and fans out to the services behind it.
         public/favicon.svg is the same mark cut out of a solid tile, which survives 16 px. -->
    <svg
      viewBox="0 0 24 24" class="size-5 shrink-0 text-primary" fill="none" stroke="currentColor"
      stroke-width="2" stroke-linecap="round" aria-hidden="true"
    >
      <path d="M2 5c5 0 6 7 10 7M2 12h10M2 19c5 0 6-7 10-7" />
      <path d="M12 12c4 0 5-7 10-7M12 12h10M12 12c4 0 5 7 10 7" />
    </svg>
    <span class="text-base font-medium">HomeProxy</span>
  </span>
</template>
