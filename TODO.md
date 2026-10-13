# To do

- [ ] **Update the GitHub Actions versions in the CI workflow.** `actions/checkout@v4` and `actions/setup-python@v5` in `.github/workflows/ci.yml` are built for Node.js 20, which GitHub is phasing out; the runner currently forces them onto Node.js 24 and prints a deprecation notice on every run. Bump both to their current major versions and confirm the checks still pass.
