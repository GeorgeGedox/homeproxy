<script setup lang="ts">
import { VisArea, VisAxis, VisCrosshair, VisLine, VisTooltip, VisXYContainer } from "@unovis/vue";
import type { Bucket } from "~/utils/traffic";

const props = defineProps<{
  total: Bucket[];
  /** The picked value's requests, drawn in the accent over the total, which then turns neutral. */
  picked?: Bucket[];
  /** The bucket in words, "15 minutes", and the space between axis labels in ms. */
  per: string;
  tick: number;
}>();

interface Row { at: number; total: number; picked: number }
const rows = computed((): Row[] => props.total.map((bucket, index) => ({ at: bucket.at, total: bucket.requests, picked: props.picked?.[index]?.requests ?? 0 })));
const main = (row: Row) => (props.picked ? row.picked : row.total);
// Whole requests only: a quiet range still gets the axis 0 to 4, not 0 to 1 in fifths.
const yDomain = computed((): [number, number] => [0, Math.max(4, ...rows.value.map((row) => row.total))]);
const ticks = computed(() => roundTicks(rows.value[0]?.at ?? 0, rows.value.at(-1)?.at ?? 0, props.tick));

const DAY = 86_400_000;
const short = (at: number | Date) => props.tick < DAY
  ? new Date(at).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })
  : new Date(at).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
// Only numbers and times reach this HTML, never user input.
const tooltip = (row: Row) => {
  const count = props.picked ? `${formatCount(row.picked)} of ${formatCount(row.total)}` : formatCount(row.total);
  const soFar = row.at === rows.value.at(-1)?.at ? ", so far" : "";
  return `<span class="font-mono">${dateTime(row.at)}</span> · ${count} requests in ${props.per === "minute" ? "the minute" : props.per}${soFar}`;
};
</script>

<template>
  <!-- As tall as the class the caller gives it. -->
  <VisXYContainer :data="rows" :padding="{ top: 8 }" :y-domain="yDomain">
    <VisLine v-if="picked" :x="(row: Row) => row.at" :y="(row: Row) => row.total" color="var(--ui-border-accented)" :line-width="1" curve-type="monotoneX" />
    <VisArea :x="(row: Row) => row.at" :y="main" color="var(--ui-primary)" :opacity="0.1" line line-color="var(--ui-primary)" :line-width="1.5" curve-type="monotoneX" />
    <VisAxis type="x" :tick-format="short" :tick-values="ticks" :grid-line="false" :tick-line="false" :domain-line="false" />
    <VisAxis type="y" :num-ticks="4" :tick-format="(v: number) => formatCount(v)" :tick-line="false" :domain-line="false" />
    <VisCrosshair color="var(--ui-primary)" :template="tooltip" />
    <VisTooltip />
  </VisXYContainer>
</template>
