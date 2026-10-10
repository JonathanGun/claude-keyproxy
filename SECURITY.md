# Security

The proxy holds OAuth tokens for your Claude accounts, so security reports come first.

## Reporting a vulnerability

Report it privately with GitHub's **Report a vulnerability** button on the [Security tab](https://github.com/JonathanGun/claude-keyproxy/security). Do not open a public issue.

Include the version (`keyproxy --version`), your OS, and the steps to reproduce. Never include a real token. A made-up one shows the problem just as well.

You get an answer within 7 days.

## Supported versions

Only the latest release gets fixes.

## What is in scope

- A way to read a token out of the proxy, the key manager page or its API
- A way for another website or a remote host to use or change your keys
- Tokens written to a log, or to a file other users can read

Out of scope: any process on your own machine can use your keys through the proxy port. This is a known limitation, listed in the README.
