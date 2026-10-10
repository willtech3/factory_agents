# Bun JUnit fixtures

Captured from Bun 1.3.14 (`bun test --reporter=junit --reporter-outfile=...`) on a two-file synthetic fixture
(`src/catalog/models.test.ts` with nested `describe` blocks, `test.skip`, `test.todo`, `test.each` and one failing test;
`other.test.js` with quotes, `&` and angle brackets in a test name). The hostname attribute is replaced with
`fixture-host`. `bun-1.3.14-junit-filtered.xml` is the same suite run with `-t "adds one"`: tests the filter left out
are reported as `<skipped />`.
