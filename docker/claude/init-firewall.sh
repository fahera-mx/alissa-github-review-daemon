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
#
# The allowlist is shared by BOTH container roles (issue #73): a bridge-executor
# service runs the same kind of unattended agent session a reviewer does, so the
# hosts it needs — the Alissa API it polls, api.anthropic.com, and GitHub for the
# repo work a job does — are the ones already listed. `firewall_domains` exists so
# that is ASSERTED rather than assumed: sourcing this file defines the list
# without touching iptables, and tests-entrypoint-executor.sh checks it.
# =============================================================================
set -euo pipefail

# The hosts the loop actually talks to, one per line. Extend via
# ALISSA_FIREWALL_EXTRA (space-separated hostnames) for private registries or
# self-hosted GitHub.
firewall_domains() {
  printf '%s\n' \
    api.github.com \
    github.com \
    codeload.github.com \
    objects.githubusercontent.com \
    api.anthropic.com \
    share.alissa.app \
    skills.alissa.app \
    api.alissa.app \
    registry.npmjs.org \
    pypi.org \
    files.pythonhosted.org \
    deb.nodesource.com \
    cli.github.com
  # Unquoted on purpose: this variable is a space-separated LIST.
  # shellcheck disable=SC2086
  for extra in ${ALISSA_FIREWALL_EXTRA:-}; do
    printf '%s\n' "${extra}"
  done
}

init_firewall() {
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

  while IFS= read -r domain; do
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
  done < <(firewall_domains)

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
}

# Direct execution raises the firewall; sourcing only defines the functions, so
# the allowlist can be asserted in a test without root or NET_ADMIN.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  init_firewall
fi
