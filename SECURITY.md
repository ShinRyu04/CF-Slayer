# Security Policy

## Scope

`cf-slayer` drives a browser to obtain Cloudflare clearance cookies and
Turnstile tokens. It is intended for security research and testing systems you
own or are authorized to test. Using it against third-party sites may violate
their terms of service or local law; that responsibility belongs to the
operator.

## Reporting a vulnerability

Do not open a public issue for a vulnerability. Report it privately through
GitHub Security Advisories for this repository:

<https://github.com/Zyrex/cf-slayer/security/advisories/new>

Include the affected component (Python service, `client-go`, or scripts), a
minimal reproduction, and the impact you believe it has. You will get an
acknowledgement and a fix or mitigation plan before any public disclosure.

## Out of scope

- Cloudflare blocking a specific site or changing its challenge.
- Sites refusing automation by design.
- Credentials or tokens found in logs of a misconfigured deployment.
