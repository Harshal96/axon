from __future__ import annotations

import anyio
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from cli.chat.adapters.base import ChatAdapter

HELP_TEXT = """/help  show commands\n/exit  leave chat"""


class ChatSession:
    def __init__(self, adapter: ChatAdapter, title: str) -> None:
        self.adapter = adapter
        self.title = title
        self.console = Console()

    def run(self) -> None:
        self.console.print(
            Panel.fit(
                (
                    f"[bold cyan]{self.title}[/bold cyan]\n"
                    "Type your query and press enter.\n"
                    f"{HELP_TEXT}"
                ),
                title="axon chat",
                border_style="cyan",
            )
        )

        while True:
            text = Prompt.ask("[bold green]you[/bold green]").strip()
            if not text:
                continue
            if text in {"/exit", "exit", "quit"}:
                self.console.print("[bold yellow]Session ended.[/bold yellow]")
                return
            if text == "/help":
                self.console.print(Panel.fit(HELP_TEXT, title="commands", border_style="blue"))
                continue

            with self.console.status("Querying axon graph...", spinner="dots"):
                payload = anyio.run(self.adapter.query, text)
            self._render_payload(payload)

    def _render_payload(self, payload: dict) -> None:
        hits = payload.get("hits") or []
        total = payload.get("total", len(hits))

        if not hits:
            self.console.print(Panel("No results found.", border_style="red", title="result"))
            return

        table = Table(title=f"Top results ({total})", show_lines=False)
        table.add_column("Name", style="bold")
        table.add_column("Kind")
        table.add_column("File")
        table.add_column("Score", justify="right")

        for hit in hits:
            table.add_row(
                str(hit.get("qualified_name") or hit.get("name") or "?"),
                str(hit.get("kind") or "?"),
                str(hit.get("file_path") or "-"),
                f"{float(hit.get('relevance_score', 0.0)):.2f}",
            )

        self.console.print(table)
