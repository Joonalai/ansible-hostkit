---
name: nls-ansible
description: NLS Ansible conventions for playbooks and roles: module choice, idempotency, variable scoping, loop structure, and secret handling.
---

## Module usage  <!-- markdownlint-disable-line MD041 -->

- Prefer Ansible modules over `command` and `shell` when an equivalent module exists.
- Use fully qualified collection names (FQCN), for example `ansible.builtin.copy`.
- Keep tasks idempotent so repeated runs converge without unintended changes.

## Variables

- Prefix role variables with the role name to avoid cross-role collisions.
- Define defaults in `defaults/main.yml` and override through inventory or play vars.
- Avoid generic variable names like `port`, `enabled`, and `timeout` inside roles.
- Guard optional variables with `is defined` checks or `default(...)` before using them in `when` conditions and templates.

## Loops and task structure

- Do not attempt nested loops in a single task.
- For outer and inner iteration, loop on `include_tasks` and run the inner loop in the included file.
- Use `loop_control.loop_var` when loop variable names could conflict.

## Failure handling

- For `command` and `shell`, define explicit `changed_when` and `failed_when` conditions.
- Do not use `ignore_errors` unless follow-up logic validates the outcome.
- Use `register` carefully and guard checks for skipped tasks before reading `.rc` or `.stdout`.
- Assume `register` values are reliably consumable only by later tasks in the same play, especially with statically included content (`import_role`/`import_tasks`).
- Treat `register` values as runtime-scoped: do not assume they are reliably available across play boundaries or immediately after dynamic includes.
- If downstream tasks must reuse a result, promote the needed value with `ansible.builtin.set_fact` (use `cacheable: true` only when persistence across separate runs is required), and read it defensively with `| default(...)`.

## Role composition

- Prefer `import_role` for static role inclusion resolved at parse time.
- Use `include_role` for conditional or looped role execution at runtime.
- Prefer static imports when downstream logic depends on registered values from included content.
- If a role is sourced from another repository and is not present during linting, list it under `mock_roles` in `.ansible-lint.yml` so `ansible-lint` does not fail on missing-role checks.
  - **Caveat**: `import_role` requires the role to already exist on disk in `roles_path` **before `ansible-playbook` starts parsing** — i.e. before any play, including one that downloads/unpacks the role. It is **not usable** for roles that are fetched at runtime by an earlier task/play in the *same* `ansible-playbook` invocation (e.g. a role unpacked from an artifact by a `download_*` role earlier in playbook). Using `import_role` there causes deploy-time "role not found" errors, and (as a side effect) also requires lint tooling to be able to resolve the role statically (e.g. via `ansible-lint`'s `mock_roles`, or a real copy of the role on disk) just to pass `--syntax-check`.
- Be explicit about tags and handler behavior when choosing dynamic role inclusion.

## Secrets

- Store credentials and sensitive values in Ansible Vault, not plaintext playbooks or vars files.
- Treat unencrypted secrets as a blocking issue and migrate them to Vault before merge.
