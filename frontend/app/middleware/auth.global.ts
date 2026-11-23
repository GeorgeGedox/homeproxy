export default defineNuxtRouteMiddleware(async (to) => {
  const session = useSession();
  const state = await session.refresh();
  const target = guardRedirect(state, to.path);
  if (target) return navigateTo(target);
});
