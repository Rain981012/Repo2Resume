# Generate PR Description

根据当前分支相对 `main`（或指定 base）的 commits + diff，生成或更新 PR 的 title/body。

## 做什么

1. 读取 `git log` / `git diff`（相对 base）
2. 写出 Summary + Test plan
3. 若用户要求「创建/更新 PR」：用 `gh pr create` 或 `gh pr edit` 写入
4. Body 末尾或开头保留标记 `<!-- auto-pr-desc -->`，以便 GitHub Action 在后续 push 时判断是否可自动重写

## 何时重写（与 CI 一致）

- **开 PR / reopen**：总是写 title（若标题很弱）和 body
- **后续 push**：仅当 body 仍含 `<!-- auto-pr-desc -->`，且新 commit **不全是** trivial（`chore`/`docs`/`style`/`ci`/`typo`/`wip` 等）时重写
- 用户删掉该 HTML 注释 = 接管 description，CI 与本命令都不应再覆盖（除非用户明确要求强制重写）

## 输出格式

```markdown
<!-- auto-pr-desc -->
## Summary
- …

## Test plan
- [ ] …
```

只输出可粘贴的 Markdown；创建 PR 前先展示给用户确认（除非用户说直接 create）。
