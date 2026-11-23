/** The GeoIP state, loaded once under one key for the Security card and every country picker. */
export function useGeoipCountries() {
  const api = useApi();
  const { data, error, refresh } = useAsyncData("geoip-countries", async () => {
    const { data, error } = await api.GET("/api/v1/geoip");
    if (!data) throw createError({ message: apiErrorMessage(error, "The GeoIP state could not be loaded.") });
    return data;
  });

  const countries = computed(() =>
    (data.value?.data?.countries ?? []).map((code) => ({ code, name: countryName(code) })).sort((a, b) => a.name.localeCompare(b.name)));
  return {
    state: data,
    error: computed(() => error.value?.message ?? ""),
    refresh,
    countries,
    global: computed(() => data.value?.block_countries ?? []),
    loaded: computed(() => data.value !== undefined),
    hasData: computed(() => Boolean(data.value?.data)),
  };
}
