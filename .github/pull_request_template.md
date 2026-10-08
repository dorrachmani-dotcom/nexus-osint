## Summary
<!-- What changed and why. Link related issues. -->

## Type
- [ ] fix
- [ ] feat
- [ ] docs
- [ ] refactor / chore / ci

## Checklist
- [ ] Commit messages follow Conventional Commits
- [ ] `pytest -q` passes; new behaviour has tests
- [ ] No secrets, `.env` contents or real personal data in code, tests or docs
- [ ] Local-only preserved: no non-loopback bind, no new unconfigured outbound calls
- [ ] Graceful degradation preserved for missing keys, tools and models
- [ ] Untrusted text rendered as text; URLs pass the SSRF guard; script data uses `|tojson`
- [ ] Docs and `CHANGELOG.md` (`Unreleased`) updated if behaviour changes
