import datetime
import asyncio
import os
import re
from collections import defaultdict
from zoneinfo import ZoneInfo
from bot.tz import resolve_tz_name
from typing import Iterable, Mapping, Any, Optional, List, Dict, Tuple


class VaultManager:
    """
    Writes bot-managed sections into an Obsidian vault via a cloud adapter (WebDAV).

    IMPORTANT:
    - Never overwrite user-authored content.
    - Only update content inside marked blocks.
    """

    TASKS_BEGIN = "<!-- BOT:BEGIN TASKS -->"
    TASKS_END = "<!-- BOT:END TASKS -->"

    BOTLOG_BEGIN = "<!-- BOT:BEGIN LOG -->"
    BOTLOG_END = "<!-- BOT:END LOG -->"

    def __init__(
        self,
        cloud_adapter,
        tz: str = "Europe/Moscow",
        *,
        objects_root: str | None = None,
        active_folder: str | None = None,
        archive_folder: str | None = None,
        project_template_paths: list[str] | None = None,
    ) -> None:
        self.cloud = cloud_adapter
        # Prefer explicit app timezone env vars over constructor arg.
        self.tz = ZoneInfo(resolve_tz_name(tz))
        # Serialize remote writes to reduce conflicts and rate-limit pressure.
        self.lock = asyncio.Lock()

        # Folder structure inside the vault (requested):
        # /Работа/Объекты/Актуальные/<project_name>.md
        # /Работа/Объекты/Архив/<project_name>.md
        self.objects_root = objects_root or os.getenv("VAULT_OBJECTS_ROOT", "/Работа/Объекты")
        self.active_folder = active_folder or os.getenv("VAULT_ACTIVE_FOLDER", "Актуальные")
        self.archive_folder = archive_folder or os.getenv("VAULT_ARCHIVE_FOLDER", "Архив")

        # Project template (Obsidian)
        # User provided: \obsidian\Templates\Объект.md
        # We try a few common vault-root variations.
        if project_template_paths is not None:
            self.project_template_paths = project_template_paths
        else:
            env_tpl = (os.getenv("VAULT_PROJECT_TEMPLATE_PATHS", "") or "").strip()
            if env_tpl:
                # Allow comma/semicolon separated lists
                parts = [p.strip() for p in re.split(r"[;,]", env_tpl) if p.strip()]
                self.project_template_paths = parts
            else:
                self.project_template_paths = [
                    "/Templates/Объект.md",
                    "/obsidian/Templates/Объект.md",
                    "/obsidian/Template/Объект.md",
                ]

    def _objects_folder_for_project_status(self, status: Optional[str]) -> str:
        """Map project status to Obsidian folder name."""
        s = (status or "").strip().lower()
        # Conservative: only explicit "done/archived/closed" goes to archive.
        if s in {"done", "archived", "closed", "archive"}:
            return self.archive_folder
        return self.active_folder

    # -------------------------
    # Helpers: blocks + rendering
    # -------------------------
    def _upsert_marked_block(
        self,
        content: str,
        begin: str,
        end: str,
        body: str,
        *,
        header: Optional[str] = None,
    ) -> str:
        """
        Insert or replace content between begin/end markers.

        If markers exist -> replace inside them.
        If not -> append new section (optionally under header).
        """
        content = content or ""

        if begin in content and end in content:
            pre, rest = content.split(begin, 1)
            _, post = rest.split(end, 1)
            return (
                pre.rstrip()
                + "\n"
                + begin
                + "\n"
                + body.rstrip()
                + "\n"
                + end
                + "\n"
                + post.lstrip()
            )

        # Markers are absent - append new block
        block = begin + "\n" + body.rstrip() + "\n" + end
        c = content.rstrip()

        if header:
            # Put under the first header occurrence if it exists, else append at end.
            if header in c:
                before, after = c.split(header, 1)
                return before + header + "\n" + block + "\n" + after.lstrip()
            return c + "\n\n" + header + "\n" + block + "\n"

        return c + "\n\n" + block + "\n"

    def _render_tasks_tree(self, tasks: Iterable[Mapping[str, Any]]) -> str:
        """
        Render nested task list in Markdown, using parent_task_id.

        Input tasks: iterable of mappings, each with at least:
          - id, title, assignee, status, deadline, parent_task_id
        """
        items: List[Mapping[str, Any]] = list(tasks or [])
        if not items:
            return "✅ Нет активных задач\n"

        task_by_id: Dict[int, Mapping[str, Any]] = {}
        children: Dict[Optional[int], List[int]] = defaultdict(list)

        for t in items:
            tid = int(t.get("id"))
            task_by_id[tid] = t
            pid = t.get("parent_task_id")
            if pid is not None:
                try:
                    pid = int(pid)
                except Exception:
                    pid = None
            children[pid].append(tid)

        def sort_key(tid: int) -> Tuple[int, datetime.datetime, int]:
            t = task_by_id.get(tid, {})
            dl = t.get("deadline")
            # Put "no deadline" last
            if dl is None:
                return (1, datetime.datetime.max.replace(tzinfo=ZoneInfo("UTC")), tid)
            # asyncpg returns naive datetime for timestamptz? depends. normalize.
            if getattr(dl, "tzinfo", None) is None:
                dl = dl.replace(tzinfo=ZoneInfo("UTC"))
            return (0, dl, tid)

        for pid, arr in list(children.items()):
            arr.sort(key=sort_key)

        # roots: parent is None or missing parent
        roots: List[int] = []
        for tid, t in task_by_id.items():
            pid = t.get("parent_task_id")
            if pid is None:
                roots.append(tid)
            else:
                try:
                    pid_int = int(pid)
                except Exception:
                    pid_int = None
                if pid_int is None or pid_int not in task_by_id:
                    roots.append(tid)

        # unique roots
        roots = sorted(set(roots), key=sort_key)

        lines: List[str] = []
        visited: set[int] = set()

        def walk(tid: int, depth: int) -> None:
            if tid in visited:
                return
            visited.add(tid)
            t = task_by_id[tid]
            status = "x" if (t.get("status") == "done") else " "
            assignee = t.get("assignee") or "—"
            title = t.get("title") or ""
            indent = "  " * depth
            dl = t.get("deadline")
            due = ""
            if dl is not None:
                if getattr(dl, "tzinfo", None) is None:
                    dl = dl.replace(tzinfo=ZoneInfo("UTC"))
                try:
                    dl_local = dl.astimezone(self.tz)
                    # Obsidian Tasks plugin syntax: 📅 YYYY-MM-DD
                    due = f" 📅 {dl_local.date().isoformat()}"
                    if dl_local.hour != 0 or dl_local.minute != 0:
                        due += f" ⏰ {dl_local.strftime('%H:%M')}"
                except Exception:
                    due = ""
            lines.append(f"{indent}- [{status}] {assignee}: {title}{due} (ID: {tid})")
            for cid in children.get(tid, []):
                walk(cid, depth + 1)

        for rid in roots:
            walk(rid, 0)

        return "\n".join(lines) + "\n"


    def _render_events(self, events: Optional[Iterable[Mapping[str, Any]]]) -> str:
        """Render recent project events into Markdown bullet list."""
        items = list(events or [])
        if not items:
            return "—\n"
        lines: List[str] = []
        for ev in items:
            ts = ev.get("created_at")
            if ts is not None and getattr(ts, "tzinfo", None) is None:
                ts = ts.replace(tzinfo=ZoneInfo("UTC"))
            try:
                ts_local = ts.astimezone(self.tz) if ts is not None else None
            except Exception:
                ts_local = None
            tstr = ts_local.strftime("%H:%M") if ts_local else ""
            txt = (ev.get("text") or "").strip()
            if not txt:
                continue
            prefix = f"- [{tstr}] " if tstr else "- "
            lines.append(prefix + txt)
        return "\n".join(lines) + "\n"

    async def _load_project_template(self, project_name: str) -> Optional[str]:
        """Load project template markdown from the vault and apply lightweight substitutions."""
        for p in self.project_template_paths:
            try:
                tpl = await self.cloud.read_file(p)
            except Exception:
                tpl = None
            if tpl:
                # Safe substitutions: only replace if placeholders exist.
                tpl = tpl.replace("{{PROJECT_NAME}}", project_name)
                tpl = tpl.replace("{{PROJECT}}", project_name)
                tpl = tpl.replace("{{TITLE}}", project_name)
                return tpl
        return None

    # -------------------------
    # Public API
    # -------------------------
    async def sync_project_file(
        self,
        project_name: str,
        tasks: Iterable[Mapping[str, Any]],
        events: Optional[Iterable[Mapping[str, Any]]] = None,
        *,
        project_status: Optional[str] = None,
    ) -> None:
        """
        Update a project's note with the current active tasks.

        Remote path convention:
        /Работа/Объекты/Актуальные/<project_name>.md
        /Работа/Объекты/Архив/<project_name>.md

        NOTE: For backward compatibility, if the file doesn't exist in the new
        structure, we also try the legacy path:
        /Работа/Объекты/<YEAR>/<project_name>.md
        """
        async with self.lock:
            folder = self._objects_folder_for_project_status(project_status)

            active_path = f"{self.objects_root}/{self.active_folder}/{project_name}.md"
            archive_path = f"{self.objects_root}/{self.archive_folder}/{project_name}.md"
            remote_path = archive_path if folder == self.archive_folder else active_path

            # Try target path first
            content = await self.cloud.read_file(remote_path)

            # If we're moving between Active/Archive, preserve user-authored content:
            # - when archiving, base on the Active note if Archive is missing
            # - when unarchiving, base on the Archive note if Active is missing
            if not content:
                if remote_path == archive_path:
                    content = await self.cloud.read_file(active_path)
                elif remote_path == active_path:
                    content = await self.cloud.read_file(archive_path)

            # Legacy fallback: objects were previously stored under year folders
            if not content:
                year = datetime.datetime.now(self.tz).year
                legacy_path = f"{self.objects_root}/{year}/{project_name}.md"
                content = await self.cloud.read_file(legacy_path)

            # If the note doesn't exist yet in Active, create it from template
            if not content and folder == self.active_folder:
                content = await self._load_project_template(project_name)

            if not content:
                content = f"# {project_name}\n"

            tasks_body = self._render_tasks_tree(tasks)

            content = self._upsert_marked_block(
                content,
                self.TASKS_BEGIN,
                self.TASKS_END,
                tasks_body,
                header="## Задачи",
            )

            # Update bot history block (recent events)
            log_body = self._render_events(events)
            content = self._upsert_marked_block(
                content,
                self.BOTLOG_BEGIN,
                self.BOTLOG_END,
                log_body,
                header="## История (бот)",
            )

            await self.cloud.upload_file(remote_path, content)

            # Cleanup stale counterpart file so the project doesn't appear in both folders.
            deleter = getattr(self.cloud, "delete_file", None)
            if callable(deleter):
                stale_path = active_path if remote_path == archive_path else archive_path
                if stale_path != remote_path:
                    try:
                        await deleter(stale_path)
                    except Exception:
                        # Non-fatal: deletion may fail on some servers/permissions.
                        pass

    async def log_event(self, event_text: str) -> None:
        """
        Append a bot event entry into a daily log note:
          /Работа/Daily/YYYY-MM-DD.md

        Note: We append to avoid rewriting large content and to reduce conflict surface.
        """
        async with self.lock:
            now = datetime.datetime.now(self.tz)
            date_str = now.strftime("%Y-%m-%d")
            remote_path = f"/Работа/Daily/{date_str}.md"

            content = await self.cloud.read_file(remote_path)
            if not content:
                content = (
                    f"---\n"
                    f"Дата заметки: \"{date_str}\"\n"
                    f"tags:\n"
                    f"---\n"
                    f"### Краткое описание\n\n"
                    f"### События бота\n"
                )

            time_str = now.strftime("%H:%M")
            content = content.rstrip() + f"\n- [{time_str}] {event_text}\n"
            await self.cloud.upload_file(remote_path, content)
