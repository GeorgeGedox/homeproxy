/** What to tell about a route that gets requests through Cloudflare's proxy without being marked proxied. Follows the route's name. */
export const cloudflareUnmarkedAdvice = "gets requests through Cloudflare's proxy, so the logs, GeoIP and CrowdSec see Cloudflare's address. Turn on its Cloudflare DNS record, proxied through Cloudflare.";
