#!/usr/bin/env bash
# =============================================================================
# Optional egress firewall — default-deny with a narrow allowlist.
#
# Adapted from the claude-code devcontainer firewall. This matters here because
# the container runs `claude` reviewer agents UNATTENDED, holding three live
# tokens, reacting to INBOUND pull requests from other accounts. Locking egress
# to the few hosts the loop actually needs limits the blast radius if a reviewed
# PR tries to talk an agent into exfiltrating.
#
# Runs as root (the entrypoint invokes it during its root bootstrap, before it
# drops to the unprivileged user) and needs --cap-add=NET_ADMIN. Gated behind
# ALISSA_ENABLE_FIREWALL=1 — off by default.
# =============================================================================
set -euo pipefail

echo "[firewall] resetting rules"
iptables -F || true
iptables -X || true
ipset destroy allowed-domains 2>/dev/null || true

# Allow loopback and established/related return traffic.
iptables -A INPUT  -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A INPUT  -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

# DNS must be allowed before we can resolve the allowlist.
iptables -A OUTPUT -p udp --dport 53 -j ACCEPT
iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT

ipset create allowed-domains hash:ip

# The hosts the review loop actually talks to. Extend via ALISSA_FIREWALL_EXTRA
# (space-separated hostnames) for private registries or self-hosted GitHub.
DOMAINS=(
  api.github.com
  github.com
  codeload.github.com
  objects.githubusercontent.com
  api.anthropic.com
  share.alissa.app
  skills.alissa.app
  api.alissa.app
  registry.npmjs.org
  pypi.org
  files.pythonhosted.org
  deb.nodesource.com
  cli.github.com
  ${ALISSA_FIREWALL_EXTRA:-}
)

for domain in "${DOMAINS[@]}"; do
  [ -n "${domain}" ] || continue
  ips="$(getent ahostsv4 "${domain}" | awk '{print $1}' | sort -u || true)"
  if [ -z "${ips}" ]; then
    echo "[firewall] WARN: could not resolve ${domain}" >&2
    continue
  fi
  for ip in ${ips}; do
    ipset add allowed-domains "${ip}" 2>/dev/null || true
  done
  echo "[firewall] allowed ${domain}"
done

# Allow egress to the allowlist; drop everything else.
iptables -A OUTPUT -m set --match-set allowed-domains dst -j ACCEPT
iptables -P INPUT   DROP
iptables -P FORWARD DROP
iptables -P OUTPUT  DROP
# Re-allow the essentials the policy would otherwise have dropped.
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -p udp --dport 53 -j ACCEPT
iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT

echo "[firewall] egress locked to allowlist"
