"""Interface Tkinter para configurar e acompanhar uma extração web."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from config import load_settings
from extrator import CrawlOptions, CrawlSummary, crawl_site


class CrawlerApp(tk.Tk):
    """Janela principal do extrator de sites."""

    POLL_INTERVAL_MS = 100

    def __init__(self) -> None:
        """Inicializa estado, widgets e eventos da janela."""

        super().__init__()
        self.title("Extrator de sites")
        self.geometry("820x620")
        self.minsize(680, 480)

        self.settings = load_settings()
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.close_requested = False

        self._build_interface()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(self.POLL_INTERVAL_MS, self._drain_events)

    def _build_interface(self) -> None:
        """Cria e posiciona os controles da aplicação."""

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        container = ttk.Frame(self, padding=16)
        container.grid(row=0, column=0, sticky="nsew")
        container.columnconfigure(1, weight=1)
        container.rowconfigure(8, weight=1)

        ttk.Label(container, text="URL inicial").grid(
            row=0,
            column=0,
            columnspan=3,
            sticky="w",
        )
        self.url_var = tk.StringVar(value=self.settings.base_url)
        self.url_entry = ttk.Entry(container, textvariable=self.url_var)
        self.url_entry.grid(
            row=1,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=(4, 12),
        )

        ttk.Label(container, text="Profundidade de rastreamento").grid(
            row=2,
            column=0,
            sticky="w",
        )
        self.depth_var = tk.StringVar(value=str(self.settings.max_depth))
        self.depth_spinbox = ttk.Spinbox(
            container,
            from_=0,
            to=20,
            textvariable=self.depth_var,
            width=8,
        )
        self.depth_spinbox.grid(row=3, column=0, sticky="w", pady=(4, 12))

        ttk.Label(container, text="Pasta dos arquivos .txt").grid(
            row=2,
            column=1,
            columnspan=2,
            sticky="w",
        )
        self.output_var = tk.StringVar(value=str(self.settings.output_dir))
        self.output_entry = ttk.Entry(container, textvariable=self.output_var)
        self.output_entry.grid(row=3, column=1, sticky="ew", padx=(12, 6), pady=(4, 12))
        self.choose_button = ttk.Button(
            container,
            text="Escolher…",
            command=self.choose_output_directory,
        )
        self.choose_button.grid(row=3, column=2, sticky="e", pady=(4, 12))

        actions = ttk.Frame(container)
        actions.grid(row=4, column=0, columnspan=3, sticky="w")
        self.start_button = ttk.Button(
            actions,
            text="Iniciar",
            command=self.start_crawl,
        )
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(
            actions,
            text="Parar",
            command=self.stop_crawl,
            state=tk.DISABLED,
        )
        self.stop_button.pack(side=tk.LEFT, padx=(8, 0))

        self.progress_label = ttk.Label(container, text="Aguardando início.")
        self.progress_label.grid(
            row=5,
            column=0,
            columnspan=3,
            sticky="w",
            pady=(16, 4),
        )
        self.progress_bar = ttk.Progressbar(
            container,
            mode="determinate",
            maximum=self.settings.max_pages,
        )
        self.progress_bar.grid(row=6, column=0, columnspan=3, sticky="ew")

        ttk.Label(container, text="Logs").grid(
            row=7,
            column=0,
            columnspan=3,
            sticky="w",
            pady=(16, 4),
        )
        log_frame = ttk.Frame(container)
        log_frame.grid(row=8, column=0, columnspan=3, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(log_frame, wrap=tk.WORD, state=tk.DISABLED)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(
            log_frame,
            orient=tk.VERTICAL,
            command=self.log_text.yview,
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def choose_output_directory(self) -> None:
        """Abre o seletor de diretório e atualiza o campo de saída."""

        initial_directory = self.output_var.get().strip() or str(Path.cwd())
        directory = filedialog.askdirectory(initialdir=initial_directory)
        if directory:
            self.output_var.set(directory)

    def append_log(self, message: str) -> None:
        """Acrescenta uma linha ao painel de logs."""

        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"{message}\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def start_crawl(self) -> None:
        """Valida os campos e inicia o crawler fora da thread gráfica."""

        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Extração em andamento", "Já existe uma extração ativa.")
            return

        try:
            depth = int(self.depth_var.get())
        except ValueError:
            messagebox.showwarning("Profundidade inválida", "Informe um número inteiro.")
            return

        output_directory = self.output_var.get().strip()
        if not output_directory:
            messagebox.showwarning("Pasta inválida", "Escolha uma pasta de saída.")
            return

        base_options = CrawlOptions.from_settings(self.settings)
        options = replace(
            base_options,
            base_url=self.url_var.get().strip(),
            output_dir=Path(output_directory).expanduser(),
            max_depth=depth,
            output_format="txt",
        )
        try:
            options.validate()
        except ValueError as error:
            messagebox.showwarning("Configuração inválida", str(error))
            return

        self.stop_event.clear()
        self.progress_bar.configure(maximum=options.max_pages, value=0)
        self.progress_label.configure(text="Preparando o navegador…")
        self._set_running_state(True)
        self.append_log(f"Extração iniciada: profundidade {depth}, saída em {options.output_dir}")

        self.worker = threading.Thread(
            target=self._run_crawler,
            args=(options,),
            name="crawler-worker",
            daemon=True,
        )
        self.worker.start()

    def _run_crawler(self, options: CrawlOptions) -> None:
        """Executa o crawler e envia eventos para a thread do Tkinter."""

        try:
            summary = crawl_site(
                options,
                stop_event=self.stop_event,
                log=lambda message: self.events.put(("log", message)),
                progress=lambda done, limit, pending: self.events.put(
                    ("progress", (done, limit, pending))
                ),
            )
        except Exception as error:
            self.events.put(("error", str(error)))
        else:
            self.events.put(("done", summary))

    def stop_crawl(self) -> None:
        """Solicita uma parada cooperativa ao crawler."""

        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self.stop_button.configure(state=tk.DISABLED)
            self.progress_label.configure(text="Interrompendo…")
            self.append_log("Parada solicitada; aguardando a operação atual terminar.")

    def _drain_events(self) -> None:
        """Transfere logs e progresso da thread de trabalho para a interface."""

        # Widgets Tk só podem ser atualizados com segurança pela thread principal.
        try:
            while True:
                event_name, payload = self.events.get_nowait()
                if event_name == "log":
                    self.append_log(str(payload))
                elif event_name == "progress":
                    done, limit, pending = payload  # type: ignore[misc]
                    self.progress_bar.configure(value=done, maximum=limit)
                    self.progress_label.configure(
                        text=f"{done} página(s) visitada(s); {pending} na fila."
                    )
                elif event_name == "done":
                    self._finish_crawl(payload)  # type: ignore[arg-type]
                elif event_name == "error":
                    self.append_log(f"Falha na extração: {payload}")
                    self.progress_label.configure(text="A extração falhou.")
                    self._set_running_state(False)
        except queue.Empty:
            pass

        if self.close_requested and not (self.worker and self.worker.is_alive()):
            self.destroy()
            return
        self.after(self.POLL_INTERVAL_MS, self._drain_events)

    def _finish_crawl(self, summary: CrawlSummary) -> None:
        """Atualiza os controles com o resultado final da execução."""

        if summary.cancelled:
            status = "Extração interrompida."
        else:
            status = "Extração concluída."
        self.progress_label.configure(
            text=(
                f"{status} {summary.saved_documents} arquivo(s) salvo(s), "
                f"{summary.downloaded_pdfs} PDF(s)."
            )
        )
        self._set_running_state(False)

    def _set_running_state(self, running: bool) -> None:
        """Habilita ou bloqueia campos conforme o estado da execução."""

        field_state = tk.DISABLED if running else tk.NORMAL
        self.url_entry.configure(state=field_state)
        self.depth_spinbox.configure(state=field_state)
        self.output_entry.configure(state=field_state)
        self.choose_button.configure(state=field_state)
        self.start_button.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_button.configure(state=tk.NORMAL if running else tk.DISABLED)

    def _on_close(self) -> None:
        """Confirma o fechamento quando existe uma extração em andamento."""

        if self.worker and self.worker.is_alive():
            should_close = messagebox.askyesno(
                "Encerrar aplicação",
                "Há uma extração em andamento. Deseja interrompê-la e sair?",
            )
            if not should_close:
                return
            self.close_requested = True
            self.stop_crawl()
            return
        self.destroy()


def main() -> None:
    """Abre a interface gráfica do extrator."""

    CrawlerApp().mainloop()


if __name__ == "__main__":
    main()
