"""Report this machine's public IP as a CIDR, ready to paste into Supabase.

    .venv\\Scripts\\python.exe whats_my_ip.py

Run it once on a PC at each warehouse — the address is the site's internet connection,
not the PC, so one run per site is enough. Paste the CIDR lines it prints into
Supabase → Project Settings → Database → Network Restrictions.
"""
import json
import socket
import sys
import urllib.request

# Several independent sources: if they disagree, something is between you and the
# internet (a VPN or proxy) and the answer would be wrong.
IPV4_SOURCES = [
    ("ipify", "https://api.ipify.org?format=json", lambda d: json.loads(d)["ip"]),
    ("icanhazip", "https://ipv4.icanhazip.com", lambda d: d.strip()),
    ("aws", "https://checkip.amazonaws.com", lambda d: d.strip()),
]
IPV6_SOURCES = [
    ("ipify", "https://api64.ipify.org?format=json", lambda d: json.loads(d)["ip"]),
]


def fetch(url, parse, timeout=10):
    request = urllib.request.Request(url, headers={"User-Agent": "inventory-setup"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return parse(response.read().decode("utf-8"))


def gather(sources):
    found = {}
    for name, url, parse in sources:
        try:
            found[name] = fetch(url, parse)
        except Exception as exc:
            found[name] = f"(unavailable: {type(exc).__name__})"
    return found


def is_ipv6(value):
    try:
        socket.inet_pton(socket.AF_INET6, value)
        return True
    except (OSError, ValueError):
        return False


def main():
    print(f"Checking the public address of this machine "
          f"({socket.gethostname()})…\n")

    print("IPv4:")
    v4 = gather(IPV4_SOURCES)
    for name, value in v4.items():
        print(f"  {name:12s} {value}")
    addresses = {v for v in v4.values() if not v.startswith("(")}

    print("\nIPv6:")
    v6 = gather(IPV6_SOURCES)
    for name, value in v6.items():
        print(f"  {name:12s} {value}")
    v6_addresses = {v for v in v6.values()
                    if not v.startswith("(") and is_ipv6(v)}

    print("\n" + "=" * 66)
    if not addresses:
        print("Could not determine a public IPv4 address. Check the internet "
              "connection, then run this again.")
        return 1

    if len(addresses) > 1:
        print("WARNING: the sources disagree —", ", ".join(sorted(addresses)))
        print("Something is rewriting your traffic (a VPN, proxy or captive portal).")
        print("Turn it off and run this again, or the allowlist will be wrong.\n")

    print("Add these to Supabase → Project Settings → Database → Network Restrictions:\n")
    for address in sorted(addresses):
        print(f"    {address}/32")
    for address in sorted(v6_addresses):
        print(f"    {address}/128")

    print("\nBefore you save:")
    print("  * Add EVERY site, including the machine you administer from, or you will")
    print("    lock yourself out of your own database.")
    print("  * If this connection has a dynamic IP — most business broadband does —")
    print("    the address will change and every PC will lose the database until you")
    print("    update the list. Ask your ISP for a static IP before relying on this.")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
