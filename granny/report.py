"""SetupReport — shared infrastructure setup status tracking + markdown reports.

Used across CDN, storage, edge, serverless, and email setup workflows to
track component status and produce structured markdown reports. Generalized
from the per-script SetupReport classes that were duplicated in 5+ files
under ``tools/create/``.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


STATUS_ICONS = {
    "created": "[NEW]",
    "exists": "[OK]",
    "configured": "[OK]",
    "requested": "[OK]",
    "uploaded": "[OK]",
    "passed": "[OK]",
    "deployed": "[OK]",
    "published": "[OK]",
    "skipped": "[SKIP]",
    "failed": "[FAIL]",
    "pending": "[ ]",
}


@dataclass
class SetupReport:
    """Infrastructure setup report with component status tracking.

    Usage::

        report = SetupReport(
            title="CDN Setup Report: example.com",
            metadata={"Domain": "example.com", "Provider": "Bunny CDN"},
            components=["storage_zone", "pull_zone", "dns", "ssl"],
        )
        report.set_status("storage_zone", "created", id=12345, region="DE")
        report.set_url("cdn", "https://example.b-cdn.net")
        report.set_env("BUNNY_PULL_ZONE_ID", "67890")
        md = report.generate_markdown()
    """

    title: str
    metadata: dict[str, str] = field(default_factory=dict)
    components: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.timestamp: str = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        self._status: dict[str, dict[str, Any]] = {
            c: {"status": "pending", "details": {}} for c in self.components
        }
        self._urls: dict[str, str] = {}
        self._env_vars: dict[str, str] = {}

    def set_status(self, component: str, status: str, **details: Any) -> None:
        if component not in self._status:
            self._status[component] = {"status": "pending", "details": {}}
        self._status[component]["status"] = status
        self._status[component]["details"].update(details)

    def set_url(self, name: str, url: str) -> None:
        self._urls[name] = url

    def set_env(self, key: str, value: str) -> None:
        self._env_vars[key] = value

    def generate_markdown(self, *, footer: str = "") -> str:
        lines = [
            f"# {self.title}",
            "",
            f"**Generated:** {self.timestamp}",
        ]
        for key, value in self.metadata.items():
            lines.append(f"**{key}:** {value}")
        lines.extend(["", "---", "", "## Component Status", ""])

        for comp_key, comp in self._status.items():
            status = comp["status"]
            icon = STATUS_ICONS.get(status, "[ ]")
            display = comp_key.replace("_", " ").title()
            lines.append(f"### {icon} {display}")
            lines.append(f"**Status:** {status.replace('_', ' ').title()}")
            for dk, dv in comp["details"].items():
                label = dk.replace("_", " ").title()
                if isinstance(dv, list):
                    lines.append(f"- **{label}:**")
                    for item in dv:
                        lines.append(f"  - `{item}`")
                else:
                    lines.append(f"- **{label}:** `{dv}`")
            lines.append("")

        if self._urls:
            lines.extend(["## URLs", "", "| Name | URL |", "|------|-----|"])
            for name, url in self._urls.items():
                lines.append(f"| {name.replace('_', ' ').title()} | {url} |")
            lines.append("")

        if footer:
            lines.extend(["---", "", footer])

        return "\n".join(lines)

    def save(self, output_dir: str | Path = ".", prefix: str = "report") -> Path:
        """Save markdown report to disk. Returns the file path."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        path = output_dir / f"{prefix}-{ts}.md"
        path.write_text(self.generate_markdown(), encoding="utf-8")
        return path

    def export_deploy_env(self, path: str | Path = ".deploy.env") -> None:
        """Write env vars to a deploy env file (append, don't overwrite)."""
        if not self._env_vars:
            return
        path = Path(path)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        with path.open("a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            for key, value in self._env_vars.items():
                if f"{key}=" not in existing:
                    f.write(f"{key}={value}\n")
