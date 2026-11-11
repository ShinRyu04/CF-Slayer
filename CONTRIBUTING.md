# Contributing

Pull requests are welcome. Keep changes small and scoped; open an issue first
for anything that changes the public contract or adds a backend.

## Local checks

Run these before opening a PR. CI runs the same commands on Ubuntu with Python
3.13 and Go 1.23.

```bash
python -m pytest tests/ -q
ruff check .
cd client-go && go test -count=1 ./... && go vet ./...
```

Tests are offline: they do not download Camoufox and do not touch real
websites. Keep them that way; the browser is only exercised by hand.

## Conventions

- Comments and Python identifiers are written in Simplified Chinese, matching
  the existing codebase.
- The public contract stays English/ASCII: JSON fields, error codes,
  environment variables, endpoint paths, SQL schema, and Go API names.
- Development happens on Windows, CI runs on Linux. Do not add
  platform-specific paths or shell assumptions to tests.

Report security issues privately as described in [SECURITY.md](SECURITY.md),
not in a public issue.

## License

By contributing, you agree that your contributions are licensed under the
GPL-3.0 License that covers this project (see [LICENSE](LICENSE)).
