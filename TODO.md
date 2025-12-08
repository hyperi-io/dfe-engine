# DFE Engine - TODO

**Project Goal:** Production-ready core library for Data Fusion Engine

---

## Current Priority

### High Priority

- [ ] Investigate cityHash64 PRIMARY KEY test failures (19 failures in schema tests)
- [ ] Review if cityHash64 addition is intentional behavior or regression
- [ ] Update test expectations or fix schema generation logic

### Medium Priority

- [ ] Add SPDX-compliant headers to all source files
- [ ] Complete integration test coverage for schema module
- [ ] Document schema generation behavior (cityHash64, ORDER BY rules)

### Low Priority

- [ ] Hunt scheduler smart query staggering (use EXPLAIN for cost estimation)
- [ ] Query cost tracking in PostgreSQL for scheduling optimization
- [ ] Performance benchmarks for schema operations

---

## Backlog

### Future Enhancements

- [ ] Storage abstraction layer (local, S3, HTTP)
- [ ] Async ClickHouse operations
- [ ] Schema diff visualization
- [ ] Hunt execution metrics dashboard

### Technical Debt

- [ ] Increase test coverage to 90%+
- [ ] Add type hints to all public APIs
- [ ] Performance profiling for large schema operations

---

## Notes

- Use `uv run pytest` for all test runs
- Docker containers persist between test runs
- See STATE.md for current project status

---

**Last Updated:** 2025-12-08
