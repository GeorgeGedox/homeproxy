/** The shell commands of the nightly backup, for the dashboard at `host` (`ip:port`). The certificate is checked, never skipped. */
export function backupCommands(host: string): string {
  const [address, port = "443"] = host.split(":");
  return [
    `# Once: save the dashboard certificate. Compare its fingerprint with the container log.`,
    `openssl s_client -connect ${host} </dev/null 2>/dev/null | openssl x509 > homeproxy.pem`,
    `openssl x509 -in homeproxy.pem -noout -fingerprint -sha256`,
    ``,
    `# Each night: the certificate names "homeproxy", so curl connects to the address with --resolve.`,
    `curl --fail --cacert homeproxy.pem --resolve homeproxy:${port}:${address} \\`,
    `  -H "Authorization: Bearer $HOMEPROXY_BACKUP_TOKEN" -OJ https://homeproxy:${port}/api/v1/backup`,
  ].join("\n");
}
