---
name: nls-python
description: NLS Python coding conventions for .py changes, including type hints, docstring and comment rules, and pytest testing patterns.
---

## Readability  <!-- markdownlint-disable-line MD041 -->

- Prefer readable, explicit code. Comprehensions and generator expressions may
  contain at most one `for` clause and one `if` clause; otherwise use explicit
  loops.
- Use blank lines within functions to separate distinct logical steps, such as
  validation, data preparation, state changes, and result handling. Keep closely
  related statements together. Avoid both uninterrupted blocks containing several
  distinct steps and blank lines between every statement.
- Keep each function's Cognitive Complexity at or below 15, using SonarQube's
  `python:S3776` definition. Reduce excessive nesting with guard clauses and
  extract cohesive responsibilities into clearly named functions. Do not
  fragment straightforward code merely to lower the score.

## Naming

- Use descriptive English function and variable names that make their purpose
  and domain context clear. Avoid generic names such as `identifier` when
  `relation_id`, `layer_id`, or `event_id` distinguishes what the value represents.
- Do not use Finnish terms or abbreviations in authored identifiers. Do not
  shorten individual words. When a long compound name must be abbreviated, use
  a recognizable three- or four-character abbreviation.
- Single-letter variables are limited to meaningful uses of `x`, `y`, `z`, `i`,
  `j`, and `k`; prefer descriptive loop variables.
- Use a single leading underscore for internal functions and methods; do not
  use double-leading underscores for privacy.
- `get_*` functions return the requested value or raise if it is absent.
  `find_*` functions return `None` when absent, or an empty iterator for iterator
  results.
- Preserve identifiers required by external APIs, schemas, and framework
  overrides.

## Type hints

- All functions must have type hints for parameters and return value.
- Use modern union syntax, for example `str | None` instead of `Optional[str]`.
- Use the `typing` module for complex type definitions.

## Comments

- Keep comments minimal.
- Public functions and methods should include a concise docstring.
- Do not add comments in tests.
- Never write comments that explain obvious code behavior; refactor the code instead.
- Never document parameters in comments; rely on type hints.
- Never use section-separator comments; extract code into functions.

## Testing

- Use module-level test functions only; do not use test classes.
- Use descriptive names in the form `test_<action>_<condition>_<expected_result>`.
- Name mock objects and variables with the `mock_` prefix. Name fixtures that
  replace behavior with a no-op using the `noop_` prefix.
- Do not add comments in test code.
- Use `pytest` fixtures for setup and teardown.
- Use `pytest.mark.parametrize` when testing multiple input values.
- For configuration-driven behavior (for example, dictionaries or lists), test only
 values that produce distinct effects where the configuration is used; do not add tests
 for values that behave identically.
- Prefer pytest-mock's `mocker.patch` over `mocker.patch.object` and
 `monkeypatch.setattr`; annotate `mocker` as `pytest_mock.MockerFixture`.
- When only observing whether or how a function or method is called, prefer
 `mocker.spy` if running its real implementation is safe and appropriate; a spy
 records calls while preserving that behavior.
- Use `mocker.patch` when the test needs to replace behavior, isolate side effects,
 or control return values or exceptions.
- Patch dependencies where they are looked up by the module under test, usually the
 imported symbol or module, rather than at their original definition.
- Use `MagicMock` imported from `unittest.mock` instead of `Mock`.
