# 工作约定（每次会话都要遵守）

## Git / 发布流程（重要）

1. **永远不要自动 `git push`**：改完代码只做本地提交，然后把变更点列给用户预览；
   用户明确说"推送/发 release"后才推。
2. **永远不要自动发 GitHub release**：只有用户明确要求时才 `gh release create`。
3. **每次改完代码后，打包本地 zip 供用户测试**（这是交付物的一部分，不算推送）：

   ```bash
   python package_release.py
   ```

   产物：`release/TuxunHelper-win64.zip`（内含 dist/ 下两个 exe + .env.example）。
   该文件已被 .gitignore 排除，仅存在于本地。
4. 若改了 `tuxun_proxy.py` / `gui.html` / `web/`，打包 zip 前需要先用
   `build_release.bat`（或其中的单条 pyinstaller 命令）重建 `dist/TuxunHelper-Realtime.exe`；
   只改 AI 模式（main.py 等）则重建 `TuxunHelper-AI.exe`。

## 其他

- 日志、config、cookie 相关文件一律不得入库（.gitignore 已覆盖）；代码与文档中不得出现用户账号 ID。
- 测试命令：`python tests/test_mirror.py`（改 tuxun_proxy.py 后必跑）。
